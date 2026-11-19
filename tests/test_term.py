"""Term-sheet signatures for offer, accept, member, and receipt."""

from __future__ import annotations

import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

import pytest

from session_credit.schema import ACCEPT_KEYS, SchemaError, cid_for, sign, verify

TS = 1_700_000_000


def seed() -> bytes:
    return os.urandom(32)


def nonce() -> str:
    return os.urandom(8).hex()


def cid() -> str:
    return "0x" + os.urandom(32).hex()


def offer_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": cid(),
        "max_latency_ms": 800,
        "flops": 1_000_000,
        "confidential": False,
        "fee": "25",
        "charge": "0",
        "draw_expiry": TS + 3_600,
        "repay_deadline": TS + 86_400,
    }
    body.update(overrides)
    return body


def did_for(raw_seed: bytes) -> str:
    return sign("offer", offer_body(), raw_seed, TS, nonce())["from"]


def body_for(kind: str) -> dict[str, Any]:
    if kind == "offer":
        return offer_body()
    if kind == "accept":
        return {"offer": cid(), "job": cid()}
    if kind == "member":
        return {"member": did_for(seed()), "expiry": TS + 86_400}
    if kind == "receipt":
        return {
            "loan": cid(),
            "job": cid(),
            "lender_amt": "1000",
            "borrower_amt": "40",
            "session": cid(),
        }
    raise AssertionError(kind)


def signed(
    kind: str,
    body: dict[str, Any],
    *,
    ts: int = TS,
    raw_seed: bytes | None = None,
) -> dict[str, Any]:
    return sign(kind, body, seed() if raw_seed is None else raw_seed, ts, nonce())


def flip_signature_bit(sig_text: str) -> str:
    pad = "=" * ((4 - len(sig_text) % 4) % 4)
    raw = bytearray(urlsafe_b64decode(sig_text + pad))
    raw[0] ^= 0x01
    flipped = urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    assert flipped != sig_text
    return flipped


def assert_too_late_to_accept(offer: dict[str, Any], now: int) -> None:
    """Caller-owned clock check. Signatures do not know the time."""
    assert offer["b"]["draw_expiry"] < now


@pytest.mark.parametrize("kind", ["offer", "accept", "member", "receipt"])
def test_sign_then_verify_round_trip(kind: str) -> None:
    body = body_for(kind)
    env = signed(kind, body)
    assert verify(env) == env
    assert env["t"] == kind
    assert env["b"] == body
    assert set(env) == {"t", "from", "ts", "n", "b", "cid", "sig"}


def test_signer_did_starts_with_ed25519_prefix() -> None:
    raw = seed()
    env = signed("offer", offer_body(), raw_seed=raw)
    assert env["from"].startswith("did:key:z6Mk")
    assert did_for(raw) == env["from"]


def test_flipped_signature_bit_is_rejected() -> None:
    env = signed("offer", offer_body())
    assert verify(env) == env
    tampered = {**env, "sig": flip_signature_bit(env["sig"])}
    with pytest.raises(SchemaError, match="signature does not match from"):
        verify(tampered)


def test_from_did_that_does_not_own_the_key_is_rejected() -> None:
    raw = seed()
    env = signed("offer", offer_body(), raw_seed=raw)
    other = did_for(seed())
    assert other != env["from"]
    assert other.startswith("did:key:z6Mk")

    swapped = {**env, "from": other}
    with pytest.raises(SchemaError, match="cid does not match the body"):
        verify(swapped)

    unsigned = {key: swapped[key] for key in ("t", "from", "ts", "n", "b")}
    resigned_cid = {**swapped, "cid": cid_for(unsigned)}
    assert resigned_cid["cid"] != env["cid"]
    assert resigned_cid["sig"] == env["sig"]
    with pytest.raises(SchemaError, match="signature does not match from"):
        verify(resigned_cid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fee", "999"),
        ("model", "0x" + "ab" * 32),
        ("repay_deadline", TS + 200_000),
    ],
)
def test_body_field_changed_after_signing_is_rejected(field: str, value: object) -> None:
    env = signed("offer", offer_body())
    assert env["b"][field] != value
    tampered = {**env, "b": {**env["b"], field: value}}
    with pytest.raises(SchemaError, match="cid does not match the body"):
        verify(tampered)


def test_accept_binds_offer_cid_and_cannot_echo_a_fee() -> None:
    offer = signed("offer", offer_body(fee="25"))
    assert verify(offer) == offer
    job = cid()
    assert job != offer["cid"]
    assert job.startswith("0x") and len(job) == 66

    accept = signed("accept", {"offer": offer["cid"], "job": job})
    verified = verify(accept)
    assert verified == accept
    assert verified["t"] == "accept"
    assert verified["b"] == {"offer": offer["cid"], "job": job}
    assert "fee" not in ACCEPT_KEYS
    assert "fee" not in verified["b"]

    other_fee = "26"
    assert other_fee != offer["b"]["fee"]
    with pytest.raises(SchemaError, match="accept fields must be exactly"):
        sign("accept", {"offer": offer["cid"], "job": job, "fee": other_fee}, seed(), TS, nonce())

    changed_fee = {**offer, "b": {**offer["b"], "fee": other_fee}}
    with pytest.raises(SchemaError, match="cid does not match the body"):
        verify(changed_fee)


def test_past_draw_expiry_still_verifies() -> None:
    now = 1_800_000_000
    draw_expiry = 1_700_003_600
    env = signed(
        "offer",
        offer_body(draw_expiry=draw_expiry, repay_deadline=draw_expiry + 86_400),
        ts=1_700_000_000,
    )
    verified = verify(env)
    assert verified == env
    assert_too_late_to_accept(verified, now)


@pytest.mark.parametrize(
    "nonce_hex",
    [
        "ABCDEF0123456789",
        "0123456789abcde",
        "0123456789abcdef00",
        "0123456789abcdeg",
        "",
    ],
)
def test_sign_rejects_nonce_that_is_not_16_lowercase_hex(nonce_hex: str) -> None:
    with pytest.raises(SchemaError, match="nonce must be 16 lowercase hex chars"):
        sign("offer", offer_body(), seed(), TS, nonce_hex)


def test_verify_rejects_nonce_that_is_not_16_lowercase_hex() -> None:
    env = signed("offer", offer_body())
    tampered = {**env, "n": "0123456789ABCDEF"}
    with pytest.raises(SchemaError, match="nonce must be 16 lowercase hex chars"):
        verify(tampered)


def test_rejects_non_positive_fee() -> None:
    good = signed("offer", offer_body(fee="1"))
    assert verify(good)["b"]["fee"] == "1"
    with pytest.raises(SchemaError, match="fee must be positive"):
        sign("offer", offer_body(fee="0"), seed(), TS, nonce())


def test_rejects_repay_deadline_before_draw_expiry() -> None:
    with pytest.raises(SchemaError, match="repay_deadline is before draw_expiry"):
        sign(
            "offer",
            offer_body(draw_expiry=1_000, repay_deadline=999),
            seed(),
            TS,
            nonce(),
        )


def test_rejects_unknown_envelope_field() -> None:
    env = signed("offer", offer_body())
    assert verify(env) == env
    tampered = {**env, "note": "extra"}
    with pytest.raises(SchemaError, match="envelope fields are wrong"):
        verify(tampered)


def test_rejects_confidential_string_false() -> None:
    good = signed("offer", offer_body(confidential=False))
    assert verify(good)["b"]["confidential"] is False
    body = offer_body()
    body["confidential"] = "false"
    with pytest.raises(SchemaError, match="confidential must be a boolean"):
        sign("offer", body, seed(), TS, nonce())
