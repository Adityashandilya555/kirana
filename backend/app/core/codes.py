"""Decision vocabulary, shared by the gate, the audit log and the UI.

`DecisionKind` values must stay in lockstep with the `check` constraint on
`decisions.kind` in sql/001_schema.sql. A mismatch is a database error at
write time, which is the right place for it to fail but an annoying one to
debug -- so the constraint list is repeated here as a docstring, not as a
second source of truth.
"""

from __future__ import annotations

from enum import StrEnum


class DecisionKind(StrEnum):
    """Mirrors decisions.kind. Keep in sync with sql/001_schema.sql."""

    CAMPAIGN_COMMITTED = "campaign_committed"
    SESSION_OPENED = "session_opened"
    INJECTION_BLOCKED = "injection_blocked"
    TOOL_CALL = "tool_call"
    PROPOSAL = "proposal"
    APPROVED = "approved"
    CLAMPED = "clamped"
    REJECTED = "rejected"
    LLM_FALLBACK = "llm_fallback"
    LLM_ERROR = "llm_error"
    ORDER_CREATED = "order_created"
    SETTLED = "settled"
    PAYMENT_FAILED = "payment_failed"
    VERIFIED = "verified"
    VERIFY_REJECTED = "verify_rejected"
    #: The agent suggested a complement, priced through the same gate.
    UPSELL = "upsell"
    #: A machine buyer was quoted. Same table as the human path on purpose:
    #: one gate, one trail, two callers.
    AGENT_QUOTE = "agent_quote"


class BoundsCode(StrEnum):
    """Why the gate decided what it decided.

    OK_* means an offer exists. Everything else means no offer is possible on
    this turn, and the customer gets a plain sentence saying so.
    """

    # -- an offer exists -----------------------------------------------------
    OK_AS_PROPOSED = "OK_AS_PROPOSED"
    OK_CLAMPED_PRODUCT_CAP = "OK_CLAMPED_PRODUCT_CAP"
    OK_CLAMPED_SLOT_CEILING = "OK_CLAMPED_SLOT_CEILING"
    OK_CLAMPED_CAMPAIGN_CEILING = "OK_CLAMPED_CAMPAIGN_CEILING"
    OK_CLAMPED_MARGIN_FLOOR = "OK_CLAMPED_MARGIN_FLOOR"
    OK_CLAMPED_BUDGET = "OK_CLAMPED_BUDGET"
    OK_CLAMPED_CUSTOMER_TIER = "OK_CLAMPED_CUSTOMER_TIER"

    # -- no offer is possible ------------------------------------------------
    TURN_LIMIT = "TURN_LIMIT"
    SLOT_NOT_OPEN = "SLOT_NOT_OPEN"
    CAMPAIGN_NOT_LIVE = "CAMPAIGN_NOT_LIVE"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    QTY_OUT_OF_RANGE = "QTY_OUT_OF_RANGE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    MARGIN_FLOOR_BLOCKS_ALL = "MARGIN_FLOOR_BLOCKS_ALL"


#: Which bound produced the granted number. Surfaced to the merchant console
#: and, on tap, to the customer.
#: These strings are a CONTRACT with the frontend: LIMIT_LABEL in
#: frontend/src/lib/plainLanguage.ts keys off them to say which limit bit, in
#: words a shopkeeper reads. A key here with no label there silently falls
#: through to a generic phrase -- which is exactly what had been happening to
#: OK_CLAMPED_BUDGET, whose value was "remaining_budget_paise" while the
#: frontend keyed "budget_paise". Every budget clamp has been rendering the
#: fallback. A test now asserts the two sets are identical.
BINDING = {
    BoundsCode.OK_CLAMPED_PRODUCT_CAP: "product_cap_bps",
    BoundsCode.OK_CLAMPED_SLOT_CEILING: "slot_ceiling_bps",
    BoundsCode.OK_CLAMPED_CAMPAIGN_CEILING: "campaign_max_discount_bps",
    BoundsCode.OK_CLAMPED_MARGIN_FLOOR: "margin_floor_bps",
    BoundsCode.OK_CLAMPED_BUDGET: "remaining_budget_paise",
    BoundsCode.OK_CLAMPED_CUSTOMER_TIER: "customer_tier_cap_bps",
}

CLAMPED_CODES = frozenset(BINDING)
OK_CODES = frozenset({BoundsCode.OK_AS_PROPOSED}) | CLAMPED_CODES
