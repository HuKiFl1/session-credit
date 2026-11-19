"""Guild treasury membership and receipt checks. No network."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from session_credit.guild import GuildError, check_receipt, require_draw
from session_credit.schema import did_from_public, sign

_TREASURY = bytes(range(32))
_BORROWER = bytes(range(32, 64))
_OTHER = bytes(range(64, 96))
_PAYER = bytes(range(96, 128))

_MODEL = "0x" + "11" * 32
_JOB = "0x" + "22" * 32
_SESSION = "0x" + "33" * 32
_OTHER_LOAN = "0x" + "44" * 32

_EXPIRY = 1_000


def _did(seed: bytes) -> str:
    private = Ed25519PrivateKey.from_private_bytes(seed)
    return did_from_public(private.public_key().public_bytes_raw())


def _offer(seed: bytes, *, fee: str = "10", charge: str = "2", nonce: str = "0123456789abcdef") -> dict:
    body = {
        "model": _MODEL,
        "max_latency_ms": 1000,
        "flops": 100,
        "confidential": False,
        "fee": fee,
        "charge": charge,
        "draw_expiry": 5_000,
        "repay_deadline": 9_000,
    }
    return sign("offer", body, seed, ts=1, nonce=nonce)


def _member(borrower: str, seed: bytes = _TREASURY, *, expiry: int = _EXPIRY) -> dict:
    return sign("member", {"member": borrower, "expiry": expiry}, seed, ts=2, nonce="abcdef0123456789")


def _receipt(offer: dict, *, lender_amt: str, loan: str | None = None, borrower_amt: str = "5") -> dict:
    body = {
        "loan": offer["cid"] if loan is None else loan,
        "job": _JOB,
        "lender_amt": lender_amt,
        "borrower_amt": borrower_amt,
        "session": _SESSION,
    }
    return sign("receipt", body, _PAYER, ts=3, nonce="0011223344556677")


def test_require_draw_succeeds_before_expiry():
    borrower = _did(_BORROWER)
    offer = _offer(_TREASURY)
    member = _member(borrower)
    require_draw(offer, member, borrower, now=_EXPIRY - 1)


def test_require_draw_rejects_other_borrower():
    borrower = _did(_BORROWER)
    other = _did(_OTHER)
    offer = _offer(_TREASURY)
    member = _member(borrower)
    with pytest.raises(GuildError):
        require_draw(offer, member, other, now=_EXPIRY - 1)


def test_require_draw_rejects_at_expiry():
    borrower = _did(_BORROWER)
    offer = _offer(_TREASURY)
    member = _member(borrower)
    with pytest.raises(GuildError):
        require_draw(offer, member, borrower, now=_EXPIRY)


def test_require_draw_rejects_other_treasury():
    borrower = _did(_BORROWER)
    offer = _offer(_OTHER, nonce="fedcba9876543210")
    member = _member(borrower)
    with pytest.raises(GuildError):
        require_draw(offer, member, borrower, now=_EXPIRY - 1)


def test_check_receipt_pays_fee_plus_charge():
    offer = _offer(_TREASURY, fee="10", charge="2")
    receipt = _receipt(offer, lender_amt="12", borrower_amt="5")
    check_receipt(offer, receipt)


def test_check_receipt_rejects_short_lender_and_wrong_loan():
    offer = _offer(_TREASURY, fee="10", charge="2")
    short = _receipt(offer, lender_amt="11", borrower_amt="5")
    with pytest.raises(GuildError):
        check_receipt(offer, short)
    wrong_loan = _receipt(offer, lender_amt="12", loan=_OTHER_LOAN)
    with pytest.raises(GuildError):
        check_receipt(offer, wrong_loan)
