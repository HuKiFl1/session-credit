"""Isolated session markets.

One market is one pinned session and one loan-to-value. Supplied FLOP is shared
inside that market and cannot fund another. The borrower receives a session key,
never a transferable balance. Rules: SPEC.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from session_credit.draw import DrawError, expire as draw_expire
from session_credit.draw import open_session, pay as draw_pay, transfer as draw_transfer
from session_credit.fakechain import FakeChain
from session_credit.loan import LoanError, reduce
from session_credit.schema import ZERO_CID, SchemaError, verify

_SESSION_FIELDS = ("model", "max_latency_ms", "flops", "confidential")


class MarketError(Exception):
    """A market message was rejected and no integer moved."""


@dataclass
class _Market:
    envelope: dict
    idle: int = 0
    outstanding: int = 0
    total_shares: int = 0
    shares: dict[str, int] = field(default_factory=dict)

    @property
    def assets(self) -> int:
        return self.idle + self.outstanding


@dataclass
class _Borrow:
    envelope: dict
    job: dict
    fee: int
    charge: int
    draw_expiry: int
    repay_deadline: int
    state: str
    miner: str | None = None
    claim: dict | None = None


@dataclass
class _Vault:
    envelope: dict
    shares: dict[str, int] = field(default_factory=dict)
    total_shares: int = 0


class Book:
    """Replay signed market messages onto one FakeChain."""

    def __init__(self, chain: FakeChain) -> None:
        if not isinstance(chain, FakeChain):
            raise MarketError("chain must be a FakeChain")
        self.chain = chain
        self.markets: dict[str, _Market] = {}
        self.borrows: dict[str, _Borrow] = {}
        self.vaults: dict[str, _Vault] = {}
        self.jobs: dict[str, dict] = {}
        self.members: dict[str, dict] = {}
        self._seen: set[str] = set()

    def replay(self, envelopes: list) -> list[str]:
        """Apply messages in (ts, cid) order. Failures are dropped."""
        ordered = sorted(envelopes, key=lambda env: (env.get("ts", 0), env.get("cid", "")))
        dropped: list[str] = []
        for envelope in ordered:
            try:
                self.apply(envelope)
            except (MarketError, SchemaError):
                cid = envelope.get("cid") if isinstance(envelope, dict) else None
                dropped.append(cid if isinstance(cid, str) else "")
        return dropped

    def apply(self, envelope: dict) -> None:
        verified = verify(envelope)
        cid = verified["cid"]
        if cid in self._seen:
            raise MarketError("message already applied")
        kind = verified["t"]
        handler = {
            "market": self._market,
            "supply": self._supply,
            "withdraw": self._withdraw,
            "job": self._job,
            "borrow": self._borrow,
            "member": self._member,
            "receipt": self._receipt,
            "vault": self._vault,
            "deposit": self._deposit,
            "redeem": self._redeem,
        }.get(kind)
        if handler is None:
            raise MarketError("message is not a market message")
        handler(verified)
        self._seen.add(cid)
        self._check_invariants()

    def pay(self, borrow_cid: str, request: dict, miner: str, now: int) -> None:
        borrow = self._borrow_row(borrow_cid)
        try:
            draw_pay(self.chain, borrow_cid, request, miner, now)
        except DrawError as exc:
            raise MarketError(str(exc)) from exc
        borrow.miner = miner
        borrow.state = self._reduce(borrow, "session_settled", now)

    def transfer(self, borrow_cid: str, to: str, amount: str) -> None:
        self._borrow_row(borrow_cid)
        try:
            draw_transfer(self.chain, borrow_cid, to, amount)
        except DrawError as exc:
            raise MarketError(str(exc)) from exc

    def expire(self, borrow_cid: str, now: int) -> None:
        borrow = self._borrow_row(borrow_cid)
        market = self.markets[borrow.envelope["b"]["market"]]
        before = self.chain.escrow_of(borrow_cid)
        try:
            draw_expire(self.chain, borrow_cid, now)
        except DrawError as exc:
            raise MarketError(str(exc)) from exc
        if before > 0 and self.chain.escrow_of(borrow_cid) == 0:
            market.idle += before
            market.outstanding -= before
            borrow.state = self._reduce(borrow, "expire", now)
        self._check_invariants()

    def window(self, borrow_cid: str, now: int, *, session_final_at: int | None = None, receipt_final_at: int | None = None) -> None:
        borrow = self._borrow_row(borrow_cid)
        borrow.state = self._reduce(
            borrow,
            "window",
            now,
            session_final_at=session_final_at,
            receipt_final_at=receipt_final_at,
        )

    def fraud(self, borrow_cid: str, now: int) -> None:
        borrow = self._borrow_row(borrow_cid)
        market = self.markets[borrow.envelope["b"]["market"]]
        next_state = self._reduce(borrow, "fraud", now)
        held = self.chain.escrow_of(borrow_cid)
        if held:
            self.chain.release_escrow(borrow_cid, market.envelope["cid"])
        elif borrow.miner is not None:
            self.chain.debit(borrow.miner, borrow.fee)
            self.chain.credit(market.envelope["cid"], str(borrow.fee))
        borrow.state = next_state
        market.idle += borrow.fee
        market.outstanding -= borrow.fee
        self._check_invariants()

    def default(self, borrow_cid: str, now: int) -> None:
        borrow = self._borrow_row(borrow_cid)
        market = self.markets[borrow.envelope["b"]["market"]]
        next_state = self._reduce(borrow, "deadline", now)
        if next_state != "defaulted":
            borrow.state = next_state
            return
        held = self.chain.escrow_of(borrow_cid)
        if held:
            self.chain.void_escrow(borrow_cid)
        borrow.state = next_state
        market.outstanding -= borrow.fee
        member = borrow.envelope["b"]["member"]
        borrow.claim = {"job": borrow.job["cid"], "member": None if member == ZERO_CID else member}
        self._check_invariants()

    def _market(self, envelope: dict) -> None:
        self.markets[envelope["cid"]] = _Market(envelope)

    def _supply(self, envelope: dict) -> None:
        market = self._require_market(envelope["b"]["market"])
        body = market.envelope["b"]
        if body["guild"] and envelope["from"] != body["curator"]:
            raise MarketError("only the curator may supply a guild market")
        amount = int(envelope["b"]["amount"])
        minted = self._preview_mint(market, amount)
        if self.chain.balance(envelope["from"]) < amount:
            raise MarketError("insufficient liquid")
        self.chain.debit(envelope["from"], amount)
        self.chain.credit(market.envelope["cid"], str(amount))
        market.idle += amount
        market.total_shares += minted
        market.shares[envelope["from"]] = market.shares.get(envelope["from"], 0) + minted

    def _withdraw(self, envelope: dict) -> None:
        market = self._require_market(envelope["b"]["market"])
        shares = int(envelope["b"]["shares"])
        held = market.shares.get(envelope["from"], 0)
        if shares > held or market.total_shares <= 0:
            raise MarketError("share balance is too small")
        out = shares * market.assets // market.total_shares
        if out <= 0 or out > market.idle:
            raise MarketError("withdraw exceeds idle")
        market_id = market.envelope["cid"]
        if self.chain.balance(market_id) < out:
            raise MarketError("market cash is short")
        self.chain.debit(market_id, out)
        self.chain.credit(envelope["from"], str(out))
        market.idle -= out
        market.total_shares -= shares
        market.shares[envelope["from"]] = held - shares

    def _job(self, envelope: dict) -> None:
        self.jobs[envelope["cid"]] = envelope

    def _member(self, envelope: dict) -> None:
        self.members[envelope["cid"]] = envelope

    def _borrow(self, envelope: dict) -> None:
        market = self._require_market(envelope["b"]["market"])
        job = self.jobs.get(envelope["b"]["job"])
        if job is None:
            raise MarketError("unknown job")
        params = market.envelope["b"]
        if any(job["b"][field] != params[field] for field in _SESSION_FIELDS):
            raise MarketError("job does not match the market")
        fee = int(envelope["b"]["fee"])
        if fee > market.idle:
            raise MarketError("fee exceeds idle")
        charge = fee * params["charge_bps"] // 10_000
        budget = int(job["b"]["budget"])
        if (fee + charge) * 10_000 > budget * params["lltv_bps"]:
            raise MarketError("borrow exceeds lltv")
        self._check_member(envelope, params)
        ts = envelope["ts"]
        draw_expiry = ts + params["draw_window_ms"]
        repay_deadline = ts + params["repay_window_ms"]
        state = reduce(None, "offer", ts, draw_expiry=draw_expiry, repay_deadline=repay_deadline)
        state = reduce(state, "accept", ts, draw_expiry=draw_expiry, repay_deadline=repay_deadline)
        state = reduce(state, "draw", ts, draw_expiry=draw_expiry, repay_deadline=repay_deadline)
        market_id = market.envelope["cid"]
        try:
            self.chain.lock_escrow(market_id, envelope["cid"], fee)
        except ValueError as exc:
            raise MarketError(str(exc)) from exc
        pinned = {field: params[field] for field in _SESSION_FIELDS}
        pinned["fee"] = envelope["b"]["fee"]
        try:
            open_session(self.chain, envelope["cid"], market_id, envelope["from"], draw_expiry, pinned)
        except DrawError as exc:
            self.chain.release_escrow(envelope["cid"], market_id)
            raise MarketError(str(exc)) from exc
        market.idle -= fee
        market.outstanding += fee
        self.borrows[envelope["cid"]] = _Borrow(
            envelope, job, fee, charge, draw_expiry, repay_deadline, state
        )

    def _receipt(self, envelope: dict) -> None:
        borrow = self.borrows.get(envelope["b"]["loan"])
        if borrow is None:
            raise MarketError("unknown borrow")
        if borrow.state != "session_final":
            raise MarketError("receipt is not due")
        owed = borrow.fee + borrow.charge
        if int(envelope["b"]["lender_amt"]) != owed:
            raise MarketError("lender amount must equal fee plus charge")
        market = self.markets[borrow.envelope["b"]["market"]]
        if self.chain.balance(envelope["from"]) < owed:
            raise MarketError("payer cannot cover the receipt")
        self.chain.debit(envelope["from"], owed)
        self.chain.credit(market.envelope["cid"], str(owed))
        market.outstanding -= borrow.fee
        market.idle += owed
        borrow.state = self._reduce(borrow, "receipt", envelope["ts"])

    def _vault(self, envelope: dict) -> None:
        self.vaults[envelope["cid"]] = _Vault(envelope)

    def _deposit(self, envelope: dict) -> None:
        vault = self.vaults.get(envelope["b"]["vault"])
        if vault is None:
            raise MarketError("unknown vault")
        amount = int(envelope["b"]["amount"])
        if self.chain.balance(envelope["from"]) < amount:
            raise MarketError("insufficient liquid")
        self.chain.debit(envelope["from"], amount)
        self.chain.credit(vault.envelope["from"], str(amount))
        vault.total_shares += amount
        vault.shares[envelope["from"]] = vault.shares.get(envelope["from"], 0) + amount

    def _redeem(self, envelope: dict) -> None:
        vault = self.vaults.get(envelope["b"]["vault"])
        if vault is None:
            raise MarketError("unknown vault")
        shares = int(envelope["b"]["shares"])
        held = vault.shares.get(envelope["from"], 0)
        if shares > held:
            raise MarketError("vault share balance is too small")
        liquid = self.chain.balance(vault.envelope["from"])
        if shares > liquid:
            raise MarketError("redeem exceeds vault liquid")
        self.chain.debit(vault.envelope["from"], shares)
        self.chain.credit(envelope["from"], str(shares))
        vault.total_shares -= shares
        vault.shares[envelope["from"]] = held - shares

    def _check_member(self, borrow: dict, params: dict) -> None:
        member_cid = borrow["b"]["member"]
        if not params["guild"]:
            if member_cid != ZERO_CID:
                raise MarketError("member must be the zero cid")
            return
        member = self.members.get(member_cid)
        if member is None:
            raise MarketError("unknown member")
        if member["from"] != params["curator"]:
            raise MarketError("member treasury is not the curator")
        if member["b"]["member"] != borrow["from"]:
            raise MarketError("member does not name the borrower")
        if borrow["ts"] >= member["b"]["expiry"]:
            raise MarketError("member message has expired")

    def _preview_mint(self, market: _Market, amount: int) -> int:
        if market.total_shares == 0:
            if market.assets != 0:
                raise MarketError("total shares are zero while assets remain")
            return amount
        minted = amount * market.total_shares // market.assets
        if minted <= 0:
            raise MarketError("mint is zero")
        return minted

    def _require_market(self, cid: str) -> _Market:
        market = self.markets.get(cid)
        if market is None:
            raise MarketError("unknown market")
        return market

    def _borrow_row(self, cid: str) -> _Borrow:
        borrow = self.borrows.get(cid)
        if borrow is None:
            raise MarketError("unknown borrow")
        return borrow

    def _reduce(
        self,
        borrow: _Borrow,
        event: str,
        now: int,
        *,
        session_final_at: int | None = None,
        receipt_final_at: int | None = None,
    ) -> str:
        try:
            return reduce(
                borrow.state,
                event,
                now,
                draw_expiry=borrow.draw_expiry,
                repay_deadline=borrow.repay_deadline,
                session_final_at=session_final_at,
                receipt_final_at=receipt_final_at,
            )
        except LoanError as exc:
            raise MarketError(str(exc)) from exc

    def _check_invariants(self) -> None:
        for market in self.markets.values():
            if market.idle < 0 or market.outstanding < 0 or market.total_shares < 0:
                raise MarketError("negative ledger")
            if market.total_shares == 0 and market.assets != 0:
                raise MarketError("shares and assets diverged")
            if sum(market.shares.values()) != market.total_shares:
                raise MarketError("share balances do not sum")
            if self.chain.balance(market.envelope["cid"]) != market.idle:
                raise MarketError("market cash does not match idle")
