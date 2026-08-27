"""Prompt-injection screening for customer messages.

Ported from personifi-aria (src/character/sanitize.ts) with three changes the
original had no reason to make:

  1. It BLOCKS instead of substituting. Aria replaced matches with
     "[filtered]" and carried on, which is right for a chat companion. Here a
     flagged message must never reach the model at all -- the `decisions` row
     it writes has llm_provider NULL, and that null is the machine-checkable
     evidence that no model was consulted. A substitution would destroy it.

  2. Two severities. Hard categories block; soft signals (length, keyword
     density) only annotate. The original treated everything alike, which in a
     haggling context would refuse real shoppers.

  3. Domain patterns. A travel guide never had to care about "the ceiling is
     now 90%" or a raw {"discount_bps": 10000} blob.

Normalisation runs BEFORE matching. Without NFKC folding and zero-width
stripping, "ıgnore prevıous instructions" walks straight through a regex
written in ASCII.

None of this is the security boundary. bounds.check() is. This exists so the
audit trail can show an attack being refused before a token was spent, and so
the obvious attempts do not waste a turn of the session's budget.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

MAX_MESSAGE_CHARS = 500

_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff\u2060]")

# NFKC leaves these alone: dotless-i is a distinct letter, and the Cyrillic
# lookalikes are genuinely different characters. Folding them is the only way
# a regex written in ASCII sees "\u0131gnore prev\u0131ous".
_CONFUSABLES = str.maketrans({
    "\u0131": "i", "\u0406": "I", "\u0456": "i", "\u2170": "i",
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
    "\u0441": "c", "\u0455": "s", "\u0443": "y", "\u0445": "x",
    "\u04bb": "h", "\u0501": "d", "\u0261": "g",
})
_SEPARATORS = re.compile(r"[  ]")
_WHITESPACE = re.compile(r"\s+")


class Block(StrEnum):
    INSTRUCTION_OVERRIDE = "S01_INSTRUCTION_OVERRIDE"
    DELIMITER_FORGERY = "S02_DELIMITER_FORGERY"
    ROLE_MARKER = "S03_ROLE_MARKER"
    PROMPT_EXTRACTION = "S04_PROMPT_EXTRACTION"
    AUTHORITY_CLAIM = "S05_AUTHORITY_CLAIM"
    BOUND_TAMPERING = "S06_BOUND_TAMPERING"
    STRUCTURED_INJECTION = "S07_STRUCTURED_INJECTION"


# Hard categories. Any match blocks the turn.
#
# Every pattern is anchored on a word that only appears when someone is
# addressing the *system* rather than the shopkeeper. "ignore" alone is not
# enough -- "ignore the dented tin" is a normal thing to say in a shop, so the
# override pattern requires ignore + previous/prior/above/system.
_BLOCKING: list[tuple[Block, re.Pattern[str]]] = [
    (Block.INSTRUCTION_OVERRIDE, re.compile(
        r"\b(ignore|forget|disregard|override|bypass)\b[^.?!]{0,30}?"
        r"\b(previous|prior|above|earlier|initial|original|all)?\s*"
        r"\b(instructions?|prompts?|rules?|directions?|directives?|system)\b", re.I)),
    (Block.INSTRUCTION_OVERRIDE, re.compile(
        r"\bnew\s+(instruction|instructions|rule|rules|prompt)\s*[:\-]", re.I)),
    (Block.INSTRUCTION_OVERRIDE, re.compile(r"\bignore\s+everything\b", re.I)),

    (Block.DELIMITER_FORGERY, re.compile(
        r"(-{3,}\s*(end|begin)\b|\bend\s+of\s+(message|prompt|input)\b"
        r"|\bnew\s+message\s+from\b)", re.I)),
    (Block.DELIMITER_FORGERY, re.compile(r"\[\[?\s*(system|inst|sys)\s*\]?\]", re.I)),
    (Block.DELIMITER_FORGERY, re.compile(r"<<\s*sys\s*>>|<\/?system>|<\|.*?\|>", re.I)),

    (Block.ROLE_MARKER, re.compile(
        r"^\s*(system|assistant|developer|tool)\s*:", re.I | re.M)),
    (Block.ROLE_MARKER, re.compile(
        r"\byou\s+are\s+now\b|\bpretend\s+(to\s+be|you\s*'?re|you\s+are)\b"
        r"|\broleplay\s+as\b|\bjailbreak\b|\bDAN\s+mode\b", re.I)),

    (Block.PROMPT_EXTRACTION, re.compile(
        r"\b(reveal|show|print|repeat|output|tell\s+me)\b[^.?!]{0,25}?"
        r"\b(your|the)\s+(instruction|instructions|prompt|system|rules)\b", re.I)),
    # "what is the system for home delivery?" is a real question a real
    # shopper asks. Bare "system" after "the" is an ordinary noun; only
    # "your system" or an explicit "system prompt" is an extraction attempt.
    (Block.PROMPT_EXTRACTION, re.compile(
        r"\bwhat\s+(are|is)\s+your\s+(instructions?|prompts?|rules?|system)\b", re.I)),
    (Block.PROMPT_EXTRACTION, re.compile(
        r"\bwhat\s+(are|is)\s+the\s+(system\s+prompt|instructions?\s+you|rules?\s+you)\b", re.I)),

    # Domain-specific. These are the ones a travel guide never needed.
    (Block.AUTHORITY_CLAIM, re.compile(
        r"\b(i\s*'?m|i\s+am|this\s+is)\s+the\s+"
        r"(owner|manager|admin|administrator|developer|shopkeeper)\b", re.I)),
    (Block.AUTHORITY_CLAIM, re.compile(
        r"\byou\s+(have|are\s+given)\s+permission\s+to\s+(exceed|ignore|override)\b", re.I)),
    (Block.AUTHORITY_CLAIM, re.compile(
        r"\b(the\s+)?(manager|owner|admin)\s+(approved|authorised|authorized|said)\b", re.I)),

    (Block.BOUND_TAMPERING, re.compile(
        r"\b(ceiling|limit|cap|floor|budget|maximum|max\s+discount)\b"
        r"[^.?!]{0,25}?\b(is\s+now|raised|removed|lifted|set\s+to|increased\s+to|=)", re.I)),
    (Block.BOUND_TAMPERING, re.compile(
        r"\b(margin|budget)\s+(floor|limit)?\s*(is|=)\s*(now\s+)?(zero|0|unlimited)\b", re.I)),

    (Block.STRUCTURED_INJECTION, re.compile(
        r"\"?\b(proposed_)?discount_bps\"?\s*[:=]\s*\d+", re.I)),
    (Block.STRUCTURED_INJECTION, re.compile(
        r"\breply\s+only\s+with\b|\brespond\s+only\s+with\b|\boutput\s+exactly\b", re.I)),
]

# Soft signals. These annotate but never block on their own.
_SUSPICIOUS_WORDS = (
    "instruction", "directive", "override", "bypass", "unlock",
    "admin", "developer", "debug", "sudo", "root",
)
SUSPICIOUS_DENSITY_THRESHOLD = 3


@dataclass(frozen=True)
class SanitizeResult:
    ok: bool
    text: str
    code: str | None = None
    categories: tuple[str, ...] = ()
    matched: tuple[str, ...] = field(default=(), repr=False)
    truncated: bool = False
    normalised: bool = False
    soft_flags: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return not self.ok


def normalise(raw: str) -> str:
    """Fold the message to a form the patterns can actually see."""
    text = unicodedata.normalize("NFKC", raw).translate(_CONFUSABLES)
    text = _ZERO_WIDTH.sub("", text)
    text = _SEPARATORS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def _match_targets(raw: str) -> tuple[str, ...]:
    """Both readings of a zero-width character.

    Stripping it catches "i<ZWSP>gnore" (inserted mid-word); replacing it with
    a space catches "ignore<ZWSP>previous" (used instead of a space). Neither
    reading alone covers both, so patterns run against both.
    """
    stripped = normalise(raw)
    spaced = _WHITESPACE.sub(
        " ",
        _ZERO_WIDTH.sub(
            " ", unicodedata.normalize("NFKC", raw).translate(_CONFUSABLES)
        ),
    ).strip()
    return (stripped,) if spaced == stripped else (stripped, spaced)


def sanitize(raw: str) -> SanitizeResult:
    """Screen one customer message.

    Returns ok=False when the message must not reach the model. The caller is
    responsible for writing the decisions row and replying without one.
    """
    text = normalise(raw)
    truncated = len(text) > MAX_MESSAGE_CHARS
    if truncated:
        text = text[:MAX_MESSAGE_CHARS].rstrip()

    targets = _match_targets(raw)
    hits: list[tuple[Block, str]] = []
    for category, pattern in _BLOCKING:
        if any(pattern.search(t) for t in targets):
            hits.append((category, pattern.pattern))

    lowered = text.lower()
    soft: list[str] = []
    density = sum(1 for w in _SUSPICIOUS_WORDS if w in lowered)
    if density >= SUSPICIOUS_DENSITY_THRESHOLD:
        soft.append(f"suspicious_word_density={density}")
    if truncated:
        soft.append("length_truncated")

    if hits:
        categories = tuple(dict.fromkeys(c.value for c, _ in hits))
        return SanitizeResult(
            ok=False,
            text=text,
            code=categories[0],
            categories=categories,
            matched=tuple(p for _, p in hits),
            truncated=truncated,
            normalised=text != raw.strip(),
            soft_flags=tuple(soft),
        )

    return SanitizeResult(
        ok=True,
        text=text,
        truncated=truncated,
        normalised=text != raw.strip(),
        soft_flags=tuple(soft),
    )
