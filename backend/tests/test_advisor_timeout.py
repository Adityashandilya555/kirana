"""The advisor does not get a haggling deadline.

LLM_READ_TIMEOUT_S is 12 seconds, and it is deliberately aggressive because a
shopper is standing at a shelf waiting for a reply. The advisor and the
post-mortem are not that: nobody is waiting, both hand the model the whole
catalogue, and both ask for structured JSON back.

Holding them to the negotiation deadline is why "Suggest limits" reported
"The assistant is not reachable just now" on every press, while /health/llm
reported the same provider answering in 1.7 seconds. The production log said
so plainly:

    WARNING kirana.advisor advisor provider ollama_cloud failed: Request timed out.

A timeout and an outage are indistinguishable to the caller, which is what made
this look like a connectivity problem for as long as it did.
"""

from __future__ import annotations

from app.core import llm
from app.core.config import settings

SPEC = llm.ProviderSpec(
    name="test", tier=1, base_url="https://example.test/v1",
    api_key="k", model="m",
)


def test_a_negotiation_turn_keeps_the_short_deadline() -> None:
    # Unchanged, and it must stay unchanged: a shopper waiting 60 seconds for
    # a counter-offer is a worse failure than not answering at all.
    assert llm.build_chat_model(SPEC).request_timeout.read == settings.LLM_READ_TIMEOUT_S
    assert settings.LLM_READ_TIMEOUT_S <= 15


def test_the_advisor_gets_a_longer_one() -> None:
    model = llm.build_chat_model(SPEC, read_timeout_s=settings.LLM_ADVISOR_TIMEOUT_S)
    assert model.request_timeout.read == settings.LLM_ADVISOR_TIMEOUT_S
    assert settings.LLM_ADVISOR_TIMEOUT_S > settings.LLM_READ_TIMEOUT_S


def test_the_connect_timeout_is_not_relaxed() -> None:
    """A slow ANSWER is worth waiting for; an unreachable HOST is not. Three
    seconds to open a socket stays three seconds either way, so a genuinely
    dead provider still fails fast enough to fall through to the next tier."""
    model = llm.build_chat_model(SPEC, read_timeout_s=600)
    assert model.request_timeout.connect == settings.LLM_CONNECT_TIMEOUT_S


def test_omitting_the_override_changes_nothing() -> None:
    # The whole compatibility story: every existing caller passes no override.
    assert (
        llm.build_chat_model(SPEC).request_timeout.read
        == llm.build_chat_model(SPEC, read_timeout_s=None).request_timeout.read
    )
