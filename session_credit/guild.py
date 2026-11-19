"""Guild treasury gate for a session-credit draw.

The treasury DID is the ``from`` of a member message and of the offer. A draw
is allowed only while that message names the borrower and has not expired.
A job receipt pays the treasury ``fee + charge`` before the member; the
lender is the offer's ``from`` (the schema has no payee field).
"""

from __future__ import annotations

from session_credit.schema import SchemaError, verify


class GuildError(Exception):
    pass


def member_active(message: dict, borrower: str, treasury: str, now: int) -> bool:
    if not isinstance(message, dict):
        raise GuildError("member message must be a dict")
    try:
        verified = verify(message)
    except SchemaError:
        return False
    if verified["t"] != "member":
        return False
    if verified["from"] != treasury:
        return False
    body = verified["b"]
    if body["member"] != borrower:
        return False
    return now < body["expiry"]


def require_draw(offer: dict, member_message: dict, borrower: str, now: int) -> None:
    if not isinstance(offer, dict):
        raise GuildError("offer must be an envelope")
    treasury = offer.get("from")
    if not member_active(member_message, borrower, treasury, now):
        raise GuildError("member gate failed")
    if offer["from"] != member_message["from"]:
        raise GuildError("offer lender is not the member treasury")


def check_receipt(offer: dict, receipt: dict) -> None:
    if not isinstance(offer, dict) or not isinstance(receipt, dict):
        raise GuildError("offer and receipt must be envelopes")
    try:
        verified_offer = verify(offer)
        verified_receipt = verify(receipt)
    except SchemaError as exc:
        raise GuildError("receipt does not verify") from exc
    if verified_offer["t"] != "offer" or verified_receipt["t"] != "receipt":
        raise GuildError("receipt does not verify")
    if verified_receipt["b"]["loan"] != verified_offer["cid"]:
        raise GuildError("receipt loan does not match the offer")
    owed = int(verified_offer["b"]["fee"]) + int(verified_offer["b"]["charge"])
    paid = int(verified_receipt["b"]["lender_amt"])
    if paid != owed:
        raise GuildError("lender amount must equal fee plus charge")
