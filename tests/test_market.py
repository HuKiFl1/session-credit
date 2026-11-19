"""SPEC.md §9. Isolated markets on FakeChain, no network."""

from __future__ import annotations

import os

import pytest

from session_credit.fakechain import FakeChain
from session_credit.market import Book, MarketError
from session_credit.schema import ZERO_CID, sign

TS = 1_700_000_000
MODEL_A = "0x" + "ab" * 32
MODEL_B = "0x" + "cd" * 32


def seed() -> bytes:
    return os.urandom(32)


def nonce(n: int) -> str:
    return f"{n:016x}"


def did_of(raw: bytes) -> str:
    return sign("vault", {}, raw, TS, nonce(1))["from"]


def market_body(model: str = MODEL_A, *, guild: bool = False, curator: str, **overrides) -> dict:
    body = {
        "model": model,
        "max_latency_ms": 2000,
        "flops": 1000,
        "confidential": False,
        "lltv_bps": 10_000,
        "charge_bps": 0,
        "repay_window_ms": 10_000,
        "draw_window_ms": 5_000,
        "guild": guild,
        "curator": curator,
    }
    body.update(overrides)
    return body


def job_body(model: str = MODEL_A, budget: str = "100") -> dict:
    return {
        "model": model,
        "max_latency_ms": 2000,
        "flops": 1000,
        "confidential": False,
        "budget": budget,
    }


def request_for(fee: str, model: str = MODEL_A) -> dict:
    return {
        "model": model,
        "max_latency_ms": 2000,
        "flops": 1000,
        "confidential": False,
        "fee": fee,
    }


class World:
    def __init__(self) -> None:
        self.chain = FakeChain()
        self.book = Book(self.chain)
        self.n = 10
        self.supplier = seed()
        self.borrower = seed()
        self.payer = seed()
        self.miner = seed()
        self.supplier_did = did_of(self.supplier)
        self.borrower_did = did_of(self.borrower)
        self.payer_did = did_of(self.payer)
        self.miner_did = did_of(self.miner)

    def sign(self, kind: str, body: dict, raw: bytes, ts: int = TS) -> dict:
        self.n += 1
        return sign(kind, body, raw, ts, nonce(self.n))

    def fund(self, raw: bytes, amount: str) -> None:
        self.chain.credit(did_of(raw), amount)

    def open_market(self, model: str = MODEL_A, **overrides) -> dict:
        curator = overrides.pop("curator", self.supplier_did)
        guild = overrides.pop("guild", False)
        env = self.sign("market", market_body(model, guild=guild, curator=curator, **overrides), self.supplier)
        self.book.apply(env)
        return env

    def supply(self, market: dict, amount: str, raw: bytes | None = None, ts: int = TS) -> dict:
        who = self.supplier if raw is None else raw
        env = self.sign("supply", {"market": market["cid"], "amount": amount}, who, ts)
        self.book.apply(env)
        return env

    def job(self, budget: str = "100", model: str = MODEL_A, ts: int = TS) -> dict:
        env = self.sign("job", job_body(model, budget), self.payer, ts)
        self.book.apply(env)
        return env

    def borrow(self, market: dict, job: dict, fee: str, *, member: str = ZERO_CID, raw: bytes | None = None, ts: int = TS + 1) -> dict:
        who = self.borrower if raw is None else raw
        env = self.sign(
            "borrow",
            {"market": market["cid"], "job": job["cid"], "fee": fee, "member": member},
            who,
            ts,
        )
        self.book.apply(env)
        return env


def test_markets_do_not_share_idle():
    world = World()
    world.fund(world.supplier, "100")
    first = world.open_market(MODEL_A)
    second = world.open_market(MODEL_B)
    world.supply(first, "50")
    world.supply(second, "50")
    job = world.job(budget="50", model=MODEL_A)
    world.borrow(first, job, "10")
    assert world.book.markets[first["cid"]].idle == 40
    assert world.book.markets[second["cid"]].idle == 50


def test_later_supplier_receives_fewer_shares_after_a_charge():
    world = World()
    world.fund(world.supplier, "100")
    other = seed()
    world.fund(other, "100")
    world.fund(world.payer, "200")
    market = world.open_market(charge_bps=10_000, lltv_bps=10_000)
    world.supply(market, "100")
    assert world.book.markets[market["cid"]].shares[world.supplier_did] == 100
    job = world.job(budget="200")
    borrowed = world.borrow(market, job, "100", ts=TS + 1)
    world.book.pay(borrowed["cid"], request_for("100"), world.miner_did, TS + 2)
    world.book.window(borrowed["cid"], TS + 3, session_final_at=TS + 3)
    receipt = world.sign(
        "receipt",
        {
            "loan": borrowed["cid"],
            "job": job["cid"],
            "lender_amt": "200",
            "borrower_amt": "0",
            "session": borrowed["cid"],
        },
        world.payer,
        TS + 4,
    )
    world.book.apply(receipt)
    minted_before = world.book.markets[market["cid"]].total_shares
    world.supply(market, "100", raw=other, ts=TS + 5)
    minted = world.book.markets[market["cid"]].total_shares - minted_before
    assert minted < 100
    assert world.book.markets[market["cid"]].shares[did_of(other)] == minted


def test_withdraw_cannot_take_borrowed_principal():
    world = World()
    world.fund(world.supplier, "100")
    market = world.open_market()
    world.supply(market, "100")
    job = world.job(budget="100")
    world.borrow(market, job, "60")
    row = world.book.markets[market["cid"]]
    idle, outstanding, shares, liquid = row.idle, row.outstanding, row.shares[world.supplier_did], world.chain.balance(world.supplier_did)
    with pytest.raises(MarketError):
        world.book.apply(world.sign("withdraw", {"market": market["cid"], "shares": "50"}, world.supplier, TS + 2))
    assert row.idle == idle
    assert row.outstanding == outstanding
    assert row.shares[world.supplier_did] == shares
    assert world.chain.balance(world.supplier_did) == liquid
    world.book.apply(world.sign("withdraw", {"market": market["cid"], "shares": "40"}, world.supplier, TS + 3))
    assert world.chain.balance(world.supplier_did) == 40
    assert row.idle == 0
    assert row.outstanding == 60


def test_lltv_boundary():
    world = World()
    world.fund(world.supplier, "200")
    market = world.open_market(lltv_bps=5_000, charge_bps=0)
    world.supply(market, "200")
    job = world.job(budget="100")
    with pytest.raises(MarketError):
        world.borrow(market, job, "51")
    assert world.book.markets[market["cid"]].idle == 200
    accepted = world.borrow(market, job, "50", ts=TS + 2)
    assert accepted["cid"] in world.book.borrows
    assert world.book.markets[market["cid"]].idle == 150


def test_session_key_cannot_transfer_and_expire_restores_idle():
    world = World()
    world.fund(world.supplier, "100")
    market = world.open_market()
    world.supply(market, "100")
    job = world.job()
    first = world.borrow(market, job, "40")
    with pytest.raises(MarketError):
        world.book.transfer(first["cid"], world.borrower_did, "1")
    with pytest.raises(MarketError):
        world.book.pay(first["cid"], request_for("40", MODEL_B), world.miner_did, TS + 2)
    assert world.book.markets[market["cid"]].idle == 60
    world.book.expire(first["cid"], TS + 1 + 5_000)
    assert world.book.markets[market["cid"]].idle == 100
    assert world.book.markets[market["cid"]].outstanding == 0
    second_job = world.job(ts=TS + 6_000)
    second = world.borrow(market, second_job, "40", ts=TS + 6_001)
    world.book.pay(second["cid"], request_for("40"), world.miner_did, TS + 6_002)
    with pytest.raises(MarketError):
        world.book.pay(second["cid"], request_for("40"), world.miner_did, TS + 6_003)
    assert world.chain.balance(world.miner_did) == 40
    assert world.chain.balance(world.borrower_did) == 0


def test_receipt_raises_the_exchange_rate_without_minting_shares():
    world = World()
    world.fund(world.supplier, "100")
    other = seed()
    world.fund(other, "110")
    world.fund(world.payer, "110")
    market = world.open_market(charge_bps=1_000, lltv_bps=10_000)
    world.supply(market, "100")
    shares_before = world.book.markets[market["cid"]].total_shares
    job = world.job(budget="110")
    borrowed = world.borrow(market, job, "100", ts=TS + 1)
    world.book.pay(borrowed["cid"], request_for("100"), world.miner_did, TS + 2)
    world.book.window(borrowed["cid"], TS + 3, session_final_at=TS + 3)
    idle_before = world.book.markets[market["cid"]].idle
    receipt = world.sign(
        "receipt",
        {
            "loan": borrowed["cid"],
            "job": job["cid"],
            "lender_amt": "110",
            "borrower_amt": "5",
            "session": borrowed["cid"],
        },
        world.payer,
        TS + 4,
    )
    world.book.apply(receipt)
    row = world.book.markets[market["cid"]]
    assert row.idle == idle_before + 110
    assert row.total_shares == shares_before
    world.supply(market, "110", raw=other, ts=TS + 5)
    assert row.shares[did_of(other)] < 110


def test_default_reduces_only_that_markets_assets():
    world = World()
    world.fund(world.supplier, "80")
    first = world.open_market(MODEL_A, repay_window_ms=10)
    second = world.open_market(MODEL_B)
    world.supply(first, "40")
    world.supply(second, "40")
    job = world.job(budget="40", model=MODEL_A)
    borrowed = world.borrow(first, job, "40", ts=TS + 1)
    assets_before = world.book.markets[first["cid"]].assets
    other_before = world.book.markets[second["cid"]].assets
    world.book.default(borrowed["cid"], TS + 1 + 10)
    assert world.book.borrows[borrowed["cid"]].state == "defaulted"
    assert world.book.markets[first["cid"]].assets == assets_before - 40
    assert world.book.markets[second["cid"]].assets == other_before
    assert world.book.borrows[borrowed["cid"]].claim["job"] == job["cid"]


def test_refund_restores_fee_without_a_charge():
    world = World()
    world.fund(world.supplier, "30")
    market = world.open_market(charge_bps=5_000)
    world.supply(market, "30")
    job = world.job(budget="45")
    borrowed = world.borrow(market, job, "30", ts=TS + 1)
    world.book.pay(borrowed["cid"], request_for("30"), world.miner_did, TS + 2)
    world.book.fraud(borrowed["cid"], TS + 3)
    row = world.book.markets[market["cid"]]
    assert row.idle == 30
    assert row.outstanding == 0
    assert row.assets == 30
    assert row.total_shares == 30
    with pytest.raises(MarketError):
        world.book.apply(
            world.sign(
                "receipt",
                {
                    "loan": borrowed["cid"],
                    "job": job["cid"],
                    "lender_amt": "45",
                    "borrower_amt": "0",
                    "session": borrowed["cid"],
                },
                world.payer,
                TS + 4,
            )
        )


def test_guild_market_rejects_strangers_and_bad_members():
    world = World()
    stranger = seed()
    world.fund(world.supplier, "40")
    world.fund(stranger, "40")
    market = world.open_market(guild=True, curator=world.supplier_did)
    with pytest.raises(MarketError):
        world.supply(market, "10", raw=stranger)
    assert world.book.markets[market["cid"]].idle == 0
    world.supply(market, "40")
    job = world.job(budget="40")
    member = world.sign(
        "member",
        {"member": world.borrower_did, "expiry": TS + 100},
        world.supplier,
        TS,
    )
    world.book.apply(member)
    with pytest.raises(MarketError):
        world.borrow(market, job, "10", member=ZERO_CID, ts=TS + 1)
    with pytest.raises(MarketError):
        world.borrow(market, job, "10", member=member["cid"], ts=TS + 100)
    other = seed()
    wrong = world.sign("member", {"member": did_of(other), "expiry": TS + 500}, world.supplier, TS)
    world.book.apply(wrong)
    with pytest.raises(MarketError):
        world.borrow(market, job, "10", member=wrong["cid"], ts=TS + 2)
    ok = world.borrow(market, job, "10", member=member["cid"], ts=TS + 2)
    assert ok["cid"] in world.book.borrows


def test_vault_redeem_stops_at_unallocated_cash():
    world = World()
    vault_key = seed()
    world.fund(world.supplier, "25")
    vault = world.sign("vault", {}, vault_key)
    world.book.apply(vault)
    market = world.open_market(curator=did_of(vault_key))
    deposited = world.sign("deposit", {"vault": vault["cid"], "amount": "25"}, world.supplier)
    world.book.apply(deposited)
    world.supply(market, "25", raw=vault_key)
    with pytest.raises(MarketError):
        world.book.apply(world.sign("redeem", {"vault": vault["cid"], "shares": "1"}, world.supplier, TS + 2))
    assert world.chain.balance(world.supplier_did) == 0
    assert world.book.markets[market["cid"]].idle == 25
