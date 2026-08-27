"""Pure-logic guards for the payment layer.

The settlement race itself is exercised by tests/test_settlement.py against a
real Postgres (marked integration). What is covered here is the handful of
Razorpay-shaped details that have no database in them and that each cost a
debugging session when got wrong.
"""

from __future__ import annotations

import pytest

from app.core import rzp
from app.core.config import settings


# --------------------------------------------------------------- receipts --
def test_receipt_is_within_razorpays_forty_char_limit() -> None:
    # Over 40 characters and Razorpay rejects the order outright.
    assert len(rzp.new_receipt()) <= rzp.RECEIPT_MAX


def test_receipts_are_unique() -> None:
    # A fixed receipt survives exactly one rehearsal: the second create_order
    # with the same value is refused as a duplicate.
    assert len({rzp.new_receipt() for _ in range(200)}) == 200


def test_receipt_prefix_is_kept_when_it_fits() -> None:
    assert rzp.new_receipt("kir").startswith("kir_")


# ------------------------------------------------------------------ notes --
def test_empty_notes_arrive_as_a_list_and_are_normalised() -> None:
    # Razorpay serialises empty notes as [] -- a JSON array, not an object.
    # Indexing that as a dict raises TypeError inside a webhook background
    # task, where nothing surfaces the error.
    assert rzp.notes_dict([]) == {}


def test_populated_notes_pass_through() -> None:
    assert rzp.notes_dict({"slot_token": "ABC"}) == {"slot_token": "ABC"}


@pytest.mark.parametrize("raw", [None, "", 0, "notes"])
def test_notes_of_any_other_shape_become_an_empty_dict(raw: object) -> None:
    assert rzp.notes_dict(raw) == {}


# -------------------------------------------------------------- stub mode --
def test_stub_mode_is_never_available_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The most important assertion in this file.

    Missing Razorpay credentials in production must be a loud failure, not a
    silent switch to synthetic orders that settle without money moving.
    """
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "", raising=False)
    monkeypatch.setattr(settings, "DEMO_MODE", True, raising=False)
    monkeypatch.setattr(settings, "APP_ENV", "production", raising=False)
    assert rzp.stub_mode() is False


def test_stub_mode_is_available_locally_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "", raising=False)
    monkeypatch.setattr(settings, "DEMO_MODE", True, raising=False)
    monkeypatch.setattr(settings, "APP_ENV", "local", raising=False)
    assert rzp.stub_mode() is True


def test_configured_keys_disable_stub_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_x", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret", raising=False)
    monkeypatch.setattr(settings, "DEMO_MODE", True, raising=False)
    monkeypatch.setattr(settings, "APP_ENV", "local", raising=False)
    assert rzp.stub_mode() is False
    assert rzp.configured() is True


def test_client_refuses_rather_than_guessing_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "", raising=False)
    with pytest.raises(rzp.PaymentConfigError):
        rzp.client()


# ------------------------------------------------------- webhook signature --
def test_webhook_without_a_secret_is_rejected_outside_stub_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No secret configured and not in stub mode means we cannot verify, and
    an unverifiable webhook must not be treated as genuine."""
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_x", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret", raising=False)
    monkeypatch.setattr(settings, "DEMO_MODE", False, raising=False)
    assert rzp.verify_webhook_signature(b'{"event":"payment.captured"}', "sig") is False


def test_stub_amount_is_an_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """Floats round in ways that produce off-by-one-paise mismatches at
    settlement, which then fail AMOUNT_MISMATCH for invisible reasons."""
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "", raising=False)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "", raising=False)
    monkeypatch.setattr(settings, "DEMO_MODE", True, raising=False)
    monkeypatch.setattr(settings, "APP_ENV", "local", raising=False)
    order = rzp.create_order(15770, rzp.new_receipt(), {})
    assert isinstance(order["amount"], int)
    assert order["amount"] == 15770
    assert order["stub"] is True
