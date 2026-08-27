"""Token minting and normalisation.

Two audiences read these tokens: a phone camera, and a human squinting at a
smudged sticker under shop lighting. Both are served by Crockford base32,
which drops I, L, O and U from the alphabet so the pairs people actually
confuse (1/I/l, 0/O) cannot both be valid, and normalises the rest on the
way back in.

That normalisation is not decoration. The merchant scanner has a manual
entry box precisely because cameras fail, and the person using it will type
O for 0 at least once during a demo. `normalize_token` makes that work.

Pure module: `secrets` and `uuid4` are the only non-determinism, and every
parsing function is a pure function of its input.
"""

from __future__ import annotations

import secrets
import uuid

# Crockford base32: no I, L, O, U.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

SLOT_TOKEN_LENGTH = 10
REDEMPTION_TOKEN_LENGTH = 12

# The redemption QR is scanned by our own merchant console, so it carries a
# prefix. Anything else in the frame is rejected outright rather than being
# hopefully interpreted -- a stray product barcode must not look like a code.
REDEMPTION_PREFIX = "KIRANA1:"

# Applied after upper-casing. U is not mapped: Crockford excludes it so that
# generated tokens never spell anything unfortunate, and a typed U is more
# likely a genuine mistake than a mis-read V.
_CONFUSABLE = str.maketrans({"I": "1", "L": "1", "O": "0"})
_STRIPPED = " -\t\r\n_"


def _random(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_slot_token() -> str:
    """10 chars over a 32-symbol alphabet: ~2^50 possibilities.

    Slot tokens are printed in public on a shelf, so they are identifiers
    rather than secrets -- but they still must not be enumerable, or a
    passer-by could walk every slot in a campaign from one sticker.
    """
    return _random(SLOT_TOKEN_LENGTH)


def new_redemption_token() -> str:
    """Longer, because this one *is* a bearer credential: whoever shows it
    claims the discount."""
    return _random(REDEMPTION_TOKEN_LENGTH)


def new_salt_hex() -> str:
    """16 bytes of entropy per Merkle leaf -- what makes the committed
    ceiling hiding rather than brute-forceable."""
    return secrets.token_hex(16)


def new_receipt() -> str:
    """A Razorpay receipt: <=40 chars and unique per order.

    A fixed string works exactly once and then fails on the second
    rehearsal, which is a terrible thing to discover on demo day.
    """
    return f"kir_{uuid.uuid4().hex[:12]}"


def normalize_token(raw: str) -> str:
    """Fold a hand-typed or mis-scanned token back to canonical form.

    Upper-cases, drops separators people insert to make long codes
    readable, and maps the confusable glyphs Crockford deliberately left out
    of the alphabet.
    """
    folded = raw.strip().upper()
    for ch in _STRIPPED:
        folded = folded.replace(ch, "")
    return folded.translate(_CONFUSABLE)


def is_valid_token(raw: str, length: int) -> bool:
    token = normalize_token(raw)
    return len(token) == length and all(c in ALPHABET for c in token)


def redemption_payload(token: str) -> str:
    """What the customer's screen encodes for the merchant to scan."""
    return f"{REDEMPTION_PREFIX}{token}"


def parse_redemption_payload(raw: str) -> str | None:
    """Pull a redemption token out of a scan, or None if this is not ours.

    Accepts the bare token too, because the manual-entry box is where a
    human types what they can read off the screen, and they will not type
    the prefix.
    """
    candidate = raw.strip()
    upper = candidate.upper()
    if upper.startswith(REDEMPTION_PREFIX):
        candidate = candidate[len(REDEMPTION_PREFIX) :]
    elif ":" in candidate:
        # Some other system's payload. Do not guess.
        return None
    token = normalize_token(candidate)
    return token if is_valid_token(token, REDEMPTION_TOKEN_LENGTH) else None


def slot_url(base_url: str, token: str) -> str:
    """The printed sticker's payload.

    Upper-cased on purpose. QR alphanumeric mode covers 0-9, A-Z and a
    handful of punctuation including ':', '/', '.' and '-' -- but no
    lowercase. Staying inside it keeps a 24-slot sheet at QR version 3
    instead of 4, which is markedly easier to scan at 35mm off paper under
    shop lighting.

    Safe because hostnames are case-insensitive and the React Router route
    matches case-insensitively by default; the token is already upper-case.
    """
    return f"{base_url.rstrip('/')}/s/{token}".upper()
