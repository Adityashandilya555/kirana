"""Machine quotes: signing, canonicalisation, scope, and what must not leak.

The negative cases carry most of the weight here. A signing test that only
checks the happy path proves the library works, not that we used it correctly --
so every property below is also asserted in the direction where it should fail.
"""

from __future__ import annotations

import base64
import copy

import pytest

from app.core import merkle
from app.core.config import settings
from app.services import agent_commerce as ac

# A deterministic key, so a failure is reproducible.
SEED = base64.b64encode(bytes(range(32))).decode()

CAMPAIGN_ID = "11111111-2222-3333-4444-555555555555"
SLOT_TOKEN = "TESTSLOT01"
CEILING = 1500


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "AGENT_SIGNING_SECRET_KEY", SEED, raising=False)


@pytest.fixture
def unsigned(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "AGENT_SIGNING_SECRET_KEY", "", raising=False)


def _context(ceiling_bps: int = CEILING, catalog: list[dict] | None = None) -> dict:
    """A slot inside a real one-leaf Merkle tree, so proofs actually verify."""
    leaf = merkle.slot_leaf_hash(CAMPAIGN_ID, 0, SLOT_TOKEN, ceiling_bps, "ab" * 16)
    tree = merkle.build_tree([leaf])
    return {
        "slot": {
            "slot_token": SLOT_TOKEN,
            "status": "unused",
            "ceiling_bps": ceiling_bps,
            "leaf_index": 0,
            "leaf_hash": leaf,
            "salt_hex": "ab" * 16,
            "proof": tree.proof(0),
            "bound_sku": None,
            "shelf_id": None,
        },
        "campaign": {
            "id": CAMPAIGN_ID,
            "status": "live",
            "budget_paise": 500_000,
            "spent_paise": 0,
            "reserved_paise": 0,
            "max_discount_bps": 2000,
            "margin_floor_bps": 1200,
            "max_turns": 6,
            "merkle_root": tree.root,
            "tree_size": tree.tree_size,
            "committed_at": "2026-08-28T00:00:00+00:00",
        },
        "merchant_id": "00000000-0000-0000-0000-00000000d001",
        "catalog": catalog
        if catalog is not None
        else [
            {
                "sku": "TEA250",
                "name": "Tata Tea Gold 250g",
                "unit": "pack",
                "price_paise": 19000,
                "cost_paise": 13500,
            }
        ],
    }


# ------------------------------------------------------ canonicalisation --
def test_key_order_does_not_change_the_signed_bytes() -> None:
    """A signature over a dict whose key order varies fails intermittently for
    reasons nobody can reproduce. Both sides must serialise identically."""
    a = {"b": 2, "a": 1, "c": {"y": 2, "x": 1}}
    b = {"c": {"x": 1, "y": 2}, "a": 1, "b": 2}
    assert ac.canonical(a) == ac.canonical(b)


def test_canonical_has_no_incidental_whitespace() -> None:
    assert b" " not in ac.canonical({"a": 1, "b": "two"})


# -------------------------------------------------------------- signing --
def test_quote_verifies_against_its_own_public_key(signed) -> None:
    q = ac.build_quote(_context(), "TEA250", 1)
    assert q["signed"] is True
    assert ac.verify(
        q["quote"], q["signature"]["value"], q["signature"]["public_key"]
    )


def test_tampering_with_the_price_breaks_the_signature(signed) -> None:
    """The assertion that makes the rest of the file meaningful."""
    q = ac.build_quote(_context(), "TEA250", 1)
    forged = copy.deepcopy(q["quote"])
    forged["granted_bps"] += 1
    assert not ac.verify(
        forged, q["signature"]["value"], q["signature"]["public_key"]
    )


def test_tampering_with_the_amount_breaks_the_signature(signed) -> None:
    q = ac.build_quote(_context(), "TEA250", 1)
    forged = copy.deepcopy(q["quote"])
    forged["final_amount_paise"] = 1
    assert not ac.verify(
        forged, q["signature"]["value"], q["signature"]["public_key"]
    )


def test_a_different_key_does_not_verify(signed) -> None:
    q = ac.build_quote(_context(), "TEA250", 1)
    other = base64.b64encode(bytes(range(31, -1, -1))).decode()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pub = (
        Ed25519PrivateKey.from_private_bytes(base64.b64decode(other))
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    assert not ac.verify(
        q["quote"], q["signature"]["value"], base64.b64encode(pub).decode()
    )


def test_unconfigured_key_serves_an_honest_unsigned_quote(unsigned) -> None:
    """Unsigned is supported; unsigned-but-looking-signed is not."""
    q = ac.build_quote(_context(), "TEA250", 1)
    assert q["signed"] is False
    assert q["signature"] is None
    assert ac.public_key_b64() is None


def test_a_malformed_key_does_not_crash_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AGENT_SIGNING_SECRET_KEY", "not-base64!!", raising=False)
    q = ac.build_quote(_context(), "TEA250", 1)
    assert q["signed"] is False


# --------------------------------------------------------------- proofs --
def test_a_quote_passes_the_checks_a_buyer_would_run(signed) -> None:
    checks = ac.self_check(ac.build_quote(_context(), "TEA250", 1))
    assert checks["leaf_recomputes"]
    assert checks["proof_verifies"]
    assert checks["grant_within_ceiling"]
    assert checks["signature_verifies"]
    assert checks["ok"]


def test_self_check_rejects_a_ceiling_that_was_not_committed(signed) -> None:
    """Raising the ceiling in the payload must break the leaf recomputation --
    otherwise the proof proves nothing about the number being quoted."""
    q = ac.build_quote(_context(), "TEA250", 1)
    q["quote"]["commitment"]["ceiling_bps"] = 9000
    assert not ac.self_check(q)["leaf_recomputes"]


# ---------------------------------------------------------------- scope --
def test_a_sku_outside_the_slot_scope_is_refused() -> None:
    """The catalog passed in is already scoped by get_slot_quote_context, so an
    off-shelf sku simply is not there. Same structural guarantee as chat."""
    with pytest.raises(ac.QuoteError) as exc:
        ac.build_quote(_context(), "RICE5", 1)
    assert exc.value.code == "ITEM_NOT_AVAILABLE"


def test_refusal_does_not_reveal_what_else_the_shop_sells() -> None:
    """Telling an unauthenticated caller which products exist outside their
    scope is the leak the binding feature exists to prevent."""
    with pytest.raises(ac.QuoteError) as exc:
        ac.build_quote(_context(), "RICE5", 1)
    assert "TEA250" not in exc.value.message


def test_a_used_slot_is_refused() -> None:
    ctx = _context()
    ctx["slot"]["status"] = "redeemed"
    with pytest.raises(ac.QuoteError) as exc:
        ac.build_quote(ctx, "TEA250", 1)
    assert exc.value.code == "SLOT_NOT_OPEN"


def test_a_closed_campaign_is_refused() -> None:
    ctx = _context()
    ctx["campaign"]["status"] = "closed"
    with pytest.raises(ac.QuoteError):
        ac.build_quote(ctx, "TEA250", 1)


# ----------------------------------------------------------- the bounds --
def test_the_grant_never_exceeds_the_committed_ceiling(signed) -> None:
    q = ac.build_quote(_context(ceiling_bps=300), "TEA250", 1)
    assert q["quote"]["granted_bps"] <= 300


def test_the_margin_floor_still_binds_for_a_machine() -> None:
    """An agent asks for the ceiling every time. The floor must hold anyway --
    a machine buyer must not be able to obtain a discount a human could not."""
    ctx = _context(ceiling_bps=2000)
    ctx["catalog"] = [
        {"sku": "SUGAR1", "name": "Sugar 1kg", "unit": "pack",
         "price_paise": 4800, "cost_paise": 4300},
    ]
    with pytest.raises(ac.QuoteError) as exc:
        ac.build_quote(ctx, "SUGAR1", 1)
    assert "MARGIN" in exc.value.code


def test_quote_carries_an_expiry(signed) -> None:
    """A quote reserves no budget, so it must not stand indefinitely against a
    budget that can move underneath it."""
    q = ac.build_quote(_context(), "TEA250", 1)
    assert q["quote"]["expires_at"] > q["quote"]["issued_at"]


# ---------------------------------------------------------- no leakage --
def test_cost_price_never_appears_in_a_quote(signed) -> None:
    """The shop's margins are its own business, and this endpoint is public."""
    q = ac.build_quote(_context(), "TEA250", 1)
    blob = ac.canonical(q["quote"]).decode()
    assert "cost" not in blob.lower()
    assert "13500" not in blob


def test_money_in_a_quote_is_integer_paise(signed) -> None:
    q = ac.build_quote(_context(), "TEA250", 2)["quote"]
    for field in ("list_price_paise", "discount_paise", "final_amount_paise"):
        assert isinstance(q[field], int), field
