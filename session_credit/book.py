"""Session-credit order book.

Parties are agents. A message that fails ``verify`` is chatter and stays out
of the book. Offers are filed by model, max latency, and confidentiality.
The first verified accept of an offer turns that offer into a loan; the loan
id is the offer cid. Members and receipts are kept and do not move offers.
"""

from __future__ import annotations

import json
from typing import Any

from session_credit.schema import SchemaError, verify

_PARSE_FAIL = object()


def ingest(envelopes: list) -> dict:
    """File verified envelopes into open books, loans, members, and receipts."""
    return _collect(envelopes)


def ingest_room(records: list[dict]) -> dict:
    """Parse room lines, drop invalid JSON as chatter, then apply :func:`ingest`."""
    messages: list[Any] = []
    for record in records:
        text = record["text"] if isinstance(record, dict) else None
        if not isinstance(text, str):
            messages.append(_PARSE_FAIL)
            continue
        try:
            messages.append(json.loads(text))
        except json.JSONDecodeError:
            messages.append(_PARSE_FAIL)
    return _collect(messages)


def _book_key(offer: dict) -> str:
    body = offer["b"]
    confidential = 1 if body["confidential"] else 0
    return f"{body['model']}|{body['max_latency_ms']}|{confidential}"


def _collect(messages: list) -> dict:
    verified: list[tuple[int, dict]] = []
    chatter_at: set[int] = set()
    for index, message in enumerate(messages):
        if message is _PARSE_FAIL:
            chatter_at.add(index)
            continue
        try:
            verified.append((index, verify(message)))
        except SchemaError:
            chatter_at.add(index)

    offers: dict[str, dict] = {}
    for _, envelope in verified:
        if envelope["t"] == "offer" and envelope["cid"] not in offers:
            offers[envelope["cid"]] = envelope

    loans: dict[str, dict] = {}
    accept_reason: dict[int, str] = {}
    for index, envelope in verified:
        if envelope["t"] != "accept":
            continue
        offer_cid = envelope["b"]["offer"]
        if offer_cid not in offers:
            accept_reason[index] = "no offer"
        elif offer_cid in loans:
            accept_reason[index] = "closed"
        else:
            loans[offer_cid] = {"offer": offers[offer_cid], "accept": envelope}

    members = [envelope for _, envelope in verified if envelope["t"] == "member"]
    receipts = [envelope for _, envelope in verified if envelope["t"] == "receipt"]

    open_books: dict[str, list] = {}
    for envelope in offers.values():
        if envelope["cid"] in loans:
            continue
        open_books.setdefault(_book_key(envelope), []).append(envelope)

    dropped: list[dict[str, str]] = []
    for index in range(len(messages)):
        if index in chatter_at:
            dropped.append({"reason": "chatter"})
        elif index in accept_reason:
            dropped.append({"reason": accept_reason[index]})

    return {
        "open": open_books,
        "loans": loans,
        "members": members,
        "receipts": receipts,
        "dropped": dropped,
    }
