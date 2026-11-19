"""Book fixture: two offers in different books, one of them closed by an accept."""

import json
from pathlib import Path

import pytest

from session_credit.book import ingest, ingest_room
from session_credit.schema import SchemaError, verify

FIXTURE = Path(__file__).parent / "fixtures" / "book.json"

# Room order written by the fixture generator:
# 0 open offer, 1 loan offer, 2 matching accept, 3 extra accept,
# 4 tampered offer, 5 chatter, 6 member, 7 receipt.
_OPEN = 0
_LOAN = 1
_ACCEPT = 2
_EXTRA = 3
_TAMPER = 4
_CHATTER = 5
_MEMBER = 6
_RECEIPT = 7


def _load():
    return json.loads(FIXTURE.read_text())


def _envelope(records, index):
    return json.loads(records[index]["text"])


def _book_key(offer):
    body = offer["b"]
    return f"{body['model']}|{body['max_latency_ms']}|{1 if body['confidential'] else 0}"


def test_ingest_room_fixture():
    records = _load()
    assert records[_CHATTER]["text"] == "hello agents"
    result = ingest_room(records)

    open_offer = _envelope(records, _OPEN)
    loan_offer = _envelope(records, _LOAN)
    accept = _envelope(records, _ACCEPT)
    extra = _envelope(records, _EXTRA)
    member = _envelope(records, _MEMBER)
    receipt = _envelope(records, _RECEIPT)

    assert open_offer["b"]["confidential"] is False
    assert loan_offer["b"]["confidential"] is True
    assert open_offer["b"]["model"] != loan_offer["b"]["model"]
    assert open_offer["b"]["max_latency_ms"] != loan_offer["b"]["max_latency_ms"]

    open_key = f"{open_offer['b']['model']}|{open_offer['b']['max_latency_ms']}|0"
    loan_key = f"{loan_offer['b']['model']}|{loan_offer['b']['max_latency_ms']}|1"
    assert open_key == _book_key(open_offer)
    assert loan_key != open_key

    assert list(result["open"]) == [open_key]
    assert result["open"][open_key] == [open_offer]
    assert loan_key not in result["open"]
    assert all(env["cid"] != loan_offer["cid"] for offers in result["open"].values() for env in offers)

    assert list(result["loans"]) == [loan_offer["cid"]]
    assert result["loans"][loan_offer["cid"]] == {"offer": loan_offer, "accept": accept}
    assert extra["b"]["offer"] == loan_offer["cid"]
    assert extra["cid"] != accept["cid"]
    verify(extra)

    with pytest.raises(SchemaError):
        verify(_envelope(records, _TAMPER))

    assert [item["reason"] for item in result["dropped"]] == ["closed", "chatter", "chatter"]
    assert result["members"] == [member]
    assert result["receipts"] == [receipt]
    assert len(result["loans"]) == 1
    assert member["cid"] not in result["loans"]
    assert receipt["cid"] not in result["loans"]
    json.dumps(result)


def test_ingest_applies_the_same_rules():
    records = _load()
    envelopes = []
    for record in records:
        try:
            envelopes.append(json.loads(record["text"]))
        except json.JSONDecodeError:
            continue
    room = ingest_room(records)
    direct = ingest(envelopes)
    assert direct["open"] == room["open"]
    assert direct["loans"] == room["loans"]
    assert direct["members"] == room["members"]
    assert direct["receipts"] == room["receipts"]
    assert [item["reason"] for item in direct["dropped"]] == ["closed", "chatter"]
