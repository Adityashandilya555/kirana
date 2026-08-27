"""Token tests. The normalisation cases are the ones that save a demo."""

from __future__ import annotations

import re

import pytest

from app.core import ids


def test_alphabet_is_crockford() -> None:
    assert len(ids.ALPHABET) == 32
    assert len(set(ids.ALPHABET)) == 32
    for excluded in "ILOU":
        assert excluded not in ids.ALPHABET, f"{excluded} is confusable"


def test_slot_tokens_are_the_right_shape() -> None:
    token = ids.new_slot_token()
    assert len(token) == ids.SLOT_TOKEN_LENGTH
    assert set(token) <= set(ids.ALPHABET)


def test_tokens_do_not_repeat() -> None:
    assert len({ids.new_slot_token() for _ in range(2000)}) == 2000


def test_salt_is_sixteen_bytes_of_hex() -> None:
    salt = ids.new_salt_hex()
    assert len(salt) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", salt)


def test_receipt_fits_razorpays_forty_char_limit() -> None:
    receipts = {ids.new_receipt() for _ in range(500)}
    assert len(receipts) == 500, "a repeated receipt breaks the second rehearsal"
    assert all(len(r) <= 40 for r in receipts)


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("abcdef1234", "ABCDEF1234"),
        ("  ABCDEF1234  ", "ABCDEF1234"),
        ("ABCD-EF12-34", "ABCDEF1234"),
        ("ABCD EF12 34", "ABCDEF1234"),
        # The glyphs a human gets wrong reading a sticker.
        ("OBCDEF1234", "0BCDEF1234"),
        ("IBCDEF1234", "1BCDEF1234"),
        ("lbcdef1234", "1BCDEF1234"),
    ],
)
def test_normalisation_forgives_the_predictable_mistakes(typed: str, expected: str) -> None:
    assert ids.normalize_token(typed) == expected


def test_generated_tokens_survive_a_normalisation_round_trip() -> None:
    for _ in range(500):
        token = ids.new_slot_token()
        assert ids.normalize_token(token) == token
        assert ids.is_valid_token(token, ids.SLOT_TOKEN_LENGTH)


def test_wrong_length_is_invalid() -> None:
    assert not ids.is_valid_token("ABC", ids.SLOT_TOKEN_LENGTH)
    assert not ids.is_valid_token(ids.new_redemption_token(), ids.SLOT_TOKEN_LENGTH)


def test_u_is_not_silently_accepted() -> None:
    # U is outside the alphabet, so a token containing one is a real error,
    # not something to guess at.
    assert not ids.is_valid_token("UUUUUUUUUU", ids.SLOT_TOKEN_LENGTH)


def test_redemption_payload_round_trips() -> None:
    token = ids.new_redemption_token()
    assert ids.parse_redemption_payload(ids.redemption_payload(token)) == token


def test_redemption_payload_accepts_a_bare_typed_token() -> None:
    token = ids.new_redemption_token()
    assert ids.parse_redemption_payload(token.lower()) == token


@pytest.mark.parametrize(
    "foreign",
    [
        "https://example.com/s/ABCDEFGHJKMN",  # a slot sticker, not a redemption code
        "8901234567890",                        # a product barcode
        "upi://pay?pa=someone@bank",
        "",
        "KIRANA1:SHORT",
        "KIRANA1:" + "U" * 12,
    ],
)
def test_foreign_payloads_are_refused_not_guessed(foreign: str) -> None:
    assert ids.parse_redemption_payload(foreign) is None


def test_slot_url_stays_inside_qr_alphanumeric_mode() -> None:
    url = ids.slot_url("https://kirana.vercel.app", "ABCDEFGHJK")
    assert url == "HTTPS://KIRANA.VERCEL.APP/S/ABCDEFGHJK"
    # QR alphanumeric mode: 0-9 A-Z space $%*+-./: and nothing else. A single
    # character outside it silently pushes the whole symbol to byte mode.
    allowed = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:")
    assert set(url) <= allowed


def test_slot_url_tolerates_a_trailing_slash_on_the_base() -> None:
    assert ids.slot_url("https://x.app/", "ABCDEFGHJK") == ids.slot_url("https://x.app", "ABCDEFGHJK")
