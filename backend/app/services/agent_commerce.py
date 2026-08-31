"""Making the shop transactable by a machine buyer.

A human shopper scans a sticker and haggles. An AI buyer cannot haggle usefully
-- it wants one round trip that produces a price it can act on and, crucially,
verify. This module is that round trip.

The claim being made to a buyer is unusual and worth stating precisely:

    Before any of this existed, the shop committed to a maximum discount for
    this specific sticker and published the root of that commitment. Here is a
    quote, a proof that the sticker's ceiling was in that commitment, and a
    signature binding the quote to the shop. You can check all three without
    trusting us and without having spoken to us before.

Three design decisions carry that.

ED25519, NOT HMAC. An HMAC needs the shared secret to verify, so only we could
check it -- which defeats the entire point. A public key published in the
discovery document lets any buyer verify cold.

QUOTES DO NOT RESERVE BUDGET. Reserving on quote would let an unauthenticated
caller drain a campaign by asking for prices it never intends to pay: a denial
of service that costs the attacker nothing. So quotes are advisory and
short-lived, and POST /sessions/{id}/accept re-runs bounds.check() against live
state before anything is held. That is exactly how the human path already works
-- the model's propose_offer approval is a quote too, and accept re-gates it.
Same guarantee, same gate, second caller.

CANONICAL SERIALISATION. The signature covers sorted-key JSON with no floats.
A signature over a dict whose key order varies is a signature that fails
intermittently for reasons nobody can reproduce, and money paths are the last
place to accept that.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import bounds, merkle
from app.core.bounds import BoundsInput
from app.core.config import settings
from app.services import customer_service

log = logging.getLogger("kirana.agent")

#: How long a quote stands. Short because it is not backed by a reservation:
#: the budget can move underneath it, and accept re-gates anyway. Long enough
#: that a buyer can decide.
QUOTE_TTL_S = 120

SIG_ALG = "ed25519"


class QuoteError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------- signing --
def _load_key():
    """The signing key, or None when unconfigured.

    Unconfigured is a supported state, not a failure: quotes are still served,
    marked `signed: false`, so the endpoint is demonstrable before anyone has
    generated a key. Silently serving an unsigned quote that *looks* signed
    would be far worse than saying so.
    """
    raw = settings.AGENT_SIGNING_SECRET_KEY.strip()
    if not raw:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        seed = base64.b64decode(raw)
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as exc:  # noqa: BLE001 - a bad key must not crash the app
        log.error("AGENT_SIGNING_SECRET_KEY is set but unusable: %s", exc)
        return None


def public_key_b64() -> str | None:
    """The verifying key, base64, for the discovery document."""
    key = _load_key()
    if key is None:
        return None
    from cryptography.hazmat.primitives import serialization

    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


def canonical(payload: dict[str, Any]) -> bytes:
    """The exact bytes a signature covers.

    sort_keys because Python dict order is insertion order and a buyer
    reconstructing the payload will not match ours. separators to drop the
    whitespace json.dumps adds by default. ensure_ascii so a product name with
    a rupee sign serialises identically on both sides.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sign(payload: dict[str, Any]) -> dict[str, Any] | None:
    key = _load_key()
    if key is None:
        return None
    signature = key.sign(canonical(payload))
    return {
        "alg": SIG_ALG,
        "public_key": public_key_b64(),
        "value": base64.b64encode(signature).decode(),
    }


def verify(payload: dict[str, Any], signature_b64: str, public_b64: str) -> bool:
    """Verify a quote the way a buyer would. Used by the tests, and available
    to anyone who wants to check our own arithmetic."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        pub.verify(base64.b64decode(signature_b64), canonical(payload))
        return True
    except Exception:  # noqa: BLE001 - any failure is a failed verification
        return False


# ----------------------------------------------------------------- quoting --
def build_quote(ctx: dict[str, Any], sku: str, qty: int) -> dict[str, Any]:
    """Run the gate for a machine buyer and wrap the verdict in a proof.

    The gate here is bounds.check() -- the same pure function the chat path
    calls, given the same live campaign state. There is deliberately no second
    implementation of the rules for agents: a divergence between the two would
    be a discount an AI buyer could get that a human could not, which is the
    exact failure this project exists to prevent.
    """
    slot, campaign = ctx["slot"], ctx["campaign"]

    if campaign.get("status") != "live":
        raise QuoteError("CAMPAIGN_NOT_LIVE", "This offer has ended.")
    if slot.get("status") not in {"unused", "offered"}:
        raise QuoteError("SLOT_NOT_OPEN", "This code has already been used.")

    want = (sku or "").strip().upper()
    item = next((c for c in ctx.get("catalog") or [] if c["sku"] == want), None)
    if item is None:
        # Either the sku does not exist or this sticker is not scoped to it.
        # The message does not distinguish, because telling an unauthenticated
        # caller which products exist outside their scope is the leak the
        # binding feature exists to prevent.
        raise QuoteError(
            "ITEM_NOT_AVAILABLE",
            f"{want} is not available on this code.",
        )

    # The machine path takes the same two ceilings as the human one. Omitting
    # them here would be a discount an AI buyer could get that a human could
    # not -- the divergence this module's header calls out by name.
    product_cap, customer_cap = customer_service.caps_for_item(
        item.get("cap_bps"), (ctx.get("session") or {}).get("tier_cap_fraction_bps")
    )
    verdict = bounds.check(
        BoundsInput(
            proposed_bps=int(slot["ceiling_bps"]),  # a machine asks for the most
            price_paise=int(item["price_paise"]),
            cost_paise=int(item["cost_paise"]),
            qty=int(qty),
            slot_ceiling_bps=int(slot["ceiling_bps"]),
            slot_status=slot["status"],
            campaign_status=campaign["status"],
            campaign_max_discount_bps=int(campaign["max_discount_bps"]),
            margin_floor_bps=int(campaign["margin_floor_bps"]),
            budget_paise=int(campaign["budget_paise"]),
            spent_paise=int(campaign["spent_paise"]),
            reserved_paise=int(campaign["reserved_paise"]),
            # An agent asking for a price is not a negotiating turn.
            turn_count=0,
            max_turns=int(campaign["max_turns"]),
            product_cap_bps=product_cap,
            customer_cap_bps=customer_cap,
        )
    )

    if not verdict.approved:
        raise QuoteError(verdict.code.value, verdict.customer_reason)

    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "slot_token": slot["slot_token"],
        "sku": item["sku"],
        "qty": int(qty),
        "currency": "INR",
        "list_price_paise": int(item["price_paise"]) * int(qty),
        "granted_bps": verdict.granted_bps,
        "discount_paise": verdict.discount_paise,
        "final_amount_paise": verdict.final_amount_paise,
        "binding_constraint": verdict.binding_constraint,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=QUOTE_TTL_S)).isoformat(),
        # The opened commitment. Everything a buyer needs to recompute the leaf
        # and walk it to the published root, without asking us again.
        "commitment": {
            "merkle_root": campaign["merkle_root"],
            "tree_size": campaign["tree_size"],
            "committed_at": campaign["committed_at"],
            "campaign_id": str(campaign["id"]),
            "leaf_index": slot["leaf_index"],
            "leaf_hash": slot["leaf_hash"],
            "salt_hex": slot["salt_hex"],
            "ceiling_bps": slot["ceiling_bps"],
            "proof": slot["proof"],
        },
    }

    signature = sign(payload)
    return {
        "quote": payload,
        "signed": signature is not None,
        "signature": signature,
        # Said plainly rather than left for a buyer to infer from a missing
        # reservation and then discover at accept time.
        "note": (
            "Advisory quote. It reserves no budget and is re-checked against "
            "live campaign state when accepted."
        ),
    }


def self_check(quote: dict[str, Any]) -> dict[str, Any]:
    """What a buyer would verify, run on our own output.

    Exists so the demo can show the three checks passing, and so a test can
    assert we never ship a quote that fails its own proof.
    """
    payload = quote["quote"]
    c = payload["commitment"]

    leaf = merkle.slot_leaf_hash(
        c["campaign_id"], c["leaf_index"], payload["slot_token"],
        c["ceiling_bps"], c["salt_hex"],
    )
    leaf_ok = leaf == c["leaf_hash"]
    proof_ok = merkle.verify_proof(
        c["leaf_hash"], c["leaf_index"], c["proof"], c["merkle_root"], c["tree_size"],
    )
    within = payload["granted_bps"] <= c["ceiling_bps"]

    sig_ok = None
    if quote.get("signature"):
        sig_ok = verify(
            payload, quote["signature"]["value"], quote["signature"]["public_key"]
        )

    return {
        "leaf_recomputes": leaf_ok,
        "proof_verifies": proof_ok,
        "grant_within_ceiling": within,
        "signature_verifies": sig_ok,
        "ok": leaf_ok and proof_ok and within and (sig_ok is not False),
    }


def discovery(base_url: str, merchant: dict[str, Any], root: str | None) -> dict[str, Any]:
    """The well-known document an AI buyer reads first."""
    return {
        "protocol": "kirana.agent-commerce/0.1",
        "merchant": {
            "name": merchant.get("name"),
            "description": merchant.get("store_line"),
        },
        "currency": "INR",
        "endpoints": {
            "catalog": f"{base_url}/api/v1/agent/catalog",
            "quote": f"{base_url}/api/v1/agent/quote",
            "verify": f"{base_url}/api/v1/agent/verify",
        },
        "capabilities": {
            "bounded_discount": {
                "description": (
                    "Every discount this shop can offer is capped by a limit "
                    "committed to a Merkle root before any code was printed. A "
                    "quote carries an inclusion proof, so the bound is "
                    "verifiable without trusting the merchant."
                ),
                "commitment_root": root,
                "proof": "rfc6962-merkle",
            }
        },
        "signing": {
            "alg": SIG_ALG,
            "public_key": public_key_b64(),
            "canonicalisation": "json, sorted keys, no whitespace, ascii-escaped",
        },
        "quote_policy": {
            "ttl_seconds": QUOTE_TTL_S,
            "reserves_budget": False,
            "revalidated_on_accept": True,
        },
    }
