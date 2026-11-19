"""Draw escrow on FakeChain. No network."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from session_credit.draw import DrawError, expire, issue, pay, transfer
from session_credit.fakechain import FakeChain
from session_credit.schema import did_from_public, sign

_LENDER = bytes(range(32))
_BORROWER = bytes(range(32, 64))
_MINER = bytes(range(64, 96))

_MODEL = "0x" + "11" * 32
_OTHER_MODEL = "0x" + "22" * 32

_DRAW_EXPIRY = 5_000
_NOW = 100


def _did(seed: bytes) -> str:
    private = Ed25519PrivateKey.from_private_bytes(seed)
    return did_from_public(private.public_key().public_bytes_raw())


def _offer(*, fee: str = "10", nonce: str = "0123456789abcdef") -> dict:
    body = {
        "model": _MODEL,
        "max_latency_ms": 1000,
        "flops": 100,
        "confidential": False,
        "fee": fee,
        "charge": "2",
        "draw_expiry": _DRAW_EXPIRY,
        "repay_deadline": 9_000,
    }
    return sign("offer", body, _LENDER, ts=1, nonce=nonce)


def _request(offer: dict, **overrides: object) -> dict:
    body = offer["b"]
    request = {
        "model": body["model"],
        "max_latency_ms": body["max_latency_ms"],
        "flops": body["flops"],
        "confidential": body["confidential"],
        "fee": body["fee"],
    }
    request.update(overrides)
    return request


def _funded(amount: str = "100", fee: str = "10"):
    chain = FakeChain()
    offer = _offer(fee=fee)
    lender = offer["from"]
    borrower = _did(_BORROWER)
    miner = _did(_MINER)
    chain.credit(lender, amount)
    return chain, offer, lender, borrower, miner


def test_pay_matching_request_moves_fee_to_miner():
    chain, offer, lender, borrower, miner = _funded()
    key_id = issue(chain, offer, borrower, _NOW)
    assert chain.balance(lender) == 90
    assert chain.escrow_of(key_id) == 10
    assert chain.balance(borrower) == 0
    assert chain.balance(miner) == 0

    pay(chain, key_id, _request(offer), miner, _NOW)

    assert chain.balance(lender) == 90
    assert chain.balance(miner) == 10
    assert chain.escrow_of(key_id) == 0
    assert chain.balance(borrower) == 0


def test_transfer_raises_and_moves_nothing():
    chain, offer, lender, borrower, miner = _funded()
    key_id = issue(chain, offer, borrower, _NOW)
    before = (
        chain.balance(lender),
        chain.escrow_of(key_id),
        chain.balance(borrower),
        chain.balance(miner),
    )
    with pytest.raises(DrawError):
        transfer(chain, key_id, borrower, "10")
    assert before == (
        chain.balance(lender),
        chain.escrow_of(key_id),
        chain.balance(borrower),
        chain.balance(miner),
    )
    assert before == (90, 10, 0, 0)


def test_second_pay_raises_and_miner_stays():
    chain, offer, lender, borrower, miner = _funded()
    key_id = issue(chain, offer, borrower, _NOW)
    pay(chain, key_id, _request(offer), miner, _NOW)
    with pytest.raises(DrawError):
        pay(chain, key_id, _request(offer), miner, _NOW)
    assert chain.balance(miner) == 10
    assert chain.balance(lender) == 90
    assert chain.escrow_of(key_id) == 0
    assert chain.balance(borrower) == 0


def test_different_model_raises_and_escrow_stays():
    chain, offer, lender, borrower, miner = _funded()
    key_id = issue(chain, offer, borrower, _NOW)
    with pytest.raises(DrawError):
        pay(chain, key_id, _request(offer, model=_OTHER_MODEL), miner, _NOW)
    assert chain.escrow_of(key_id) == 10
    assert chain.balance(miner) == 0
    assert chain.balance(lender) == 90
    assert chain.balance(borrower) == 0


def test_expire_at_draw_expiry_returns_fee_to_lender():
    chain, offer, lender, borrower, miner = _funded()
    key_id = issue(chain, offer, borrower, _NOW)
    expire(chain, key_id, _DRAW_EXPIRY)
    assert chain.balance(lender) == 100
    assert chain.escrow_of(key_id) == 0
    assert chain.balance(borrower) == 0
    assert chain.balance(miner) == 0


def test_issue_rejects_when_liquid_is_short():
    chain, offer, lender, borrower, _miner = _funded(amount="9")
    with pytest.raises(DrawError):
        issue(chain, offer, borrower, _NOW)
    assert chain.balance(lender) == 9
    assert chain.escrow_of(offer["cid"]) == 0
    assert chain.balance(borrower) == 0


def test_higher_fee_raises_and_moves_nothing():
    chain, offer, lender, borrower, miner = _funded()
    key_id = issue(chain, offer, borrower, _NOW)
    with pytest.raises(DrawError):
        pay(chain, key_id, _request(offer, fee="11"), miner, _NOW)
    assert chain.escrow_of(key_id) == 10
    assert chain.balance(lender) == 90
    assert chain.balance(miner) == 0
    assert chain.balance(borrower) == 0


def test_pay_at_draw_expiry_moves_nothing():
    chain, offer, lender, borrower, miner = _funded()
    key_id = issue(chain, offer, borrower, _NOW)
    with pytest.raises(DrawError):
        pay(chain, key_id, _request(offer), miner, _DRAW_EXPIRY)
    assert chain.escrow_of(key_id) == 10
    assert chain.balance(lender) == 90
    assert chain.balance(miner) == 0
    expire(chain, key_id, _DRAW_EXPIRY)
    assert chain.balance(lender) == 100
    assert chain.escrow_of(key_id) == 0
    assert chain.balance(miner) == 0


def test_expire_after_pay_does_not_mint():
    chain, offer, lender, borrower, miner = _funded()
    key_id = issue(chain, offer, borrower, _NOW)
    pay(chain, key_id, _request(offer), miner, _NOW)
    expire(chain, key_id, _DRAW_EXPIRY)
    expire(chain, key_id, _DRAW_EXPIRY + 1)
    assert chain.balance(lender) == 90
    assert chain.balance(miner) == 10
    assert chain.escrow_of(key_id) == 0
    assert chain.balance(borrower) == 0


def test_issue_at_draw_expiry_moves_nothing():
    chain, offer, lender, borrower, _miner = _funded()
    with pytest.raises(DrawError):
        issue(chain, offer, borrower, _DRAW_EXPIRY)
    assert chain.balance(lender) == 100
    assert chain.escrow_of(offer["cid"]) == 0
    assert chain.balance(borrower) == 0


def test_issue_allows_exact_liquid():
    chain, offer, lender, borrower, _miner = _funded(amount="10")
    key_id = issue(chain, offer, borrower, _NOW)
    assert chain.balance(lender) == 0
    assert chain.escrow_of(key_id) == 10
    assert chain.balance(borrower) == 0
