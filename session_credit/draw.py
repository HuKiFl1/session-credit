"""Draw one pinned inference session from a lender's liquid balance.

The borrower never receives a transferable balance. ``issue`` escrows the
offer fee under the offer cid. That session key can pay one matching request
to a miner, or ``expire`` can return the escrow to the lender. It cannot
transfer.
"""

from __future__ import annotations

from session_credit.fakechain import FakeChain
from session_credit.schema import SchemaError, verify

_REQUEST_FIELDS = ("model", "max_latency_ms", "flops", "confidential", "fee")


class DrawError(Exception):
    """The draw was rejected and no balance moved."""


class _Session:
    def __init__(self, lender: str, borrower: str, draw_expiry: int, pinned: dict, state: str) -> None:
        self.lender = lender
        self.borrower = borrower
        self.draw_expiry = draw_expiry
        self.pinned = pinned
        self.state = state


def issue(chain: FakeChain, offer: dict, borrower: str, now: int) -> str:
    """Escrow the offer fee from the lender and return the session key id.

    The key id is the offer cid. Rejects at or after ``draw_expiry`` and when
    the lender's liquid balance is below the fee. Invalid envelopes raise
    SchemaError.
    """
    _chain(chain)
    if not isinstance(borrower, str):
        raise DrawError("borrower must be a string")
    try:
        envelope = verify(offer)
    except SchemaError:
        raise
    if envelope["t"] != "offer":
        raise DrawError("envelope must be an offer")
    now = _time(now)
    body = envelope["b"]
    if now >= body["draw_expiry"]:
        raise DrawError("draw window has closed")
    fee = int(body["fee"])
    lender = envelope["from"]
    key_id = envelope["cid"]
    book = _book(chain)
    if key_id in book:
        raise DrawError("session key already issued")
    if chain.balance(lender) < fee:
        raise DrawError("lender liquid balance is below the fee")
    pinned = {field: body[field] for field in _REQUEST_FIELDS}
    try:
        chain.lock_escrow(lender, key_id, fee)
    except ValueError as exc:
        raise DrawError(str(exc)) from exc
    book[key_id] = _Session(lender, borrower, body["draw_expiry"], pinned, "open")
    return key_id


def pay(chain: FakeChain, key_id: str, request: dict, miner: str, now: int) -> None:
    """Spend the session key: send its escrow to the miner.

    The request must carry exactly the pinned model, max_latency_ms, flops,
    confidential, and fee. A second pay, a mismatched field, or a pay at or
    after draw_expiry raises and moves nothing.
    """
    session = _open_for_read(chain, key_id)
    now = _time(now)
    if not isinstance(miner, str) or miner == "":
        raise DrawError("miner is required")
    if session.state != "open":
        raise DrawError("session key is spent")
    if now >= session.draw_expiry:
        raise DrawError("draw window has closed")
    _require_match(session.pinned, request, chain.escrow_of(key_id))
    try:
        chain.release_escrow(key_id, miner)
    except ValueError as exc:
        raise DrawError(str(exc)) from exc
    session.state = "spent"


def transfer(chain: FakeChain, key_id: str, to: str, amount: str) -> None:
    """A session key is not a transferable balance."""
    del chain, key_id, to, amount
    raise DrawError("a session key cannot transfer")


def expire(chain: FakeChain, key_id: str, now: int) -> None:
    """Return a remaining escrow to the lender at or after draw_expiry.

    Before the deadline this moves nothing. After pay, or when the escrow is
    already gone, this is a no-op and does not mint funds.
    """
    session = _open_for_read(chain, key_id)
    now = _time(now)
    if session.state == "spent" or now < session.draw_expiry:
        return
    if session.state != "open":
        return
    amount = chain.escrow_of(key_id)
    if amount <= 0:
        session.state = "expired"
        return
    try:
        chain.release_escrow(key_id, session.lender)
    except ValueError as exc:
        raise DrawError(str(exc)) from exc
    session.state = "expired"


def _chain(chain: FakeChain) -> None:
    if not isinstance(chain, FakeChain):
        raise DrawError("chain must be a FakeChain")


def _time(now: int) -> int:
    if type(now) is not int:
        raise DrawError("now must be an integer")
    return now


def _book(chain: FakeChain) -> dict[str, _Session]:
    book = getattr(chain, "_draws", None)
    if book is None:
        book = {}
        setattr(chain, "_draws", book)
    return book


def _open_for_read(chain: FakeChain, key_id: str) -> _Session:
    _chain(chain)
    if not isinstance(key_id, str) or key_id == "":
        raise DrawError("unknown session key")
    session = _book(chain).get(key_id)
    if session is None:
        raise DrawError("unknown session key")
    return session


def _require_match(pinned: dict, request: dict, escrow: int) -> None:
    if not isinstance(request, dict) or set(request) != set(_REQUEST_FIELDS):
        raise DrawError("request fields are wrong")
    for field in _REQUEST_FIELDS:
        if type(request[field]) is not type(pinned[field]) or request[field] != pinned[field]:
            raise DrawError(f"request {field} does not match the offer")
    if int(request["fee"]) != escrow:
        raise DrawError("fee must equal the escrowed fee")
