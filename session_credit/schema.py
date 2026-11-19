"""Signed session-credit messages.

Field names are frozen. Sprint code imports this module and does not rename
fields. A borrower never receives a transferable balance; that rule lives in
the draw code, not here.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_DID = re.compile(r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]+$")
_CID = re.compile(r"^0x[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{16}$")
_MODEL = re.compile(r"^0x[0-9a-f]{64}$")
_AMT = re.compile(r"^0$|^[1-9][0-9]*$")
_SIG_DOMAIN = b"session-credit-sig|"
_ED25519_PREFIX = bytes((0xED, 0x01))

ENVELOPE_KEYS = frozenset({"t", "from", "ts", "n", "b", "cid", "sig"})
TYPES = frozenset(
    {
        "offer",
        "accept",
        "member",
        "receipt",
        "market",
        "supply",
        "withdraw",
        "job",
        "borrow",
        "vault",
        "deposit",
        "redeem",
    }
)
ZERO_CID = "0x" + ("0" * 64)

OFFER_KEYS = frozenset(
    {
        "model",
        "max_latency_ms",
        "flops",
        "confidential",
        "fee",
        "charge",
        "draw_expiry",
        "repay_deadline",
    }
)
ACCEPT_KEYS = frozenset({"offer", "job"})
MEMBER_KEYS = frozenset({"member", "expiry"})
RECEIPT_KEYS = frozenset({"loan", "job", "lender_amt", "borrower_amt", "session"})
MARKET_KEYS = frozenset(
    {
        "model",
        "max_latency_ms",
        "flops",
        "confidential",
        "lltv_bps",
        "charge_bps",
        "repay_window_ms",
        "draw_window_ms",
        "guild",
        "curator",
    }
)
SUPPLY_KEYS = frozenset({"market", "amount"})
WITHDRAW_KEYS = frozenset({"market", "shares"})
JOB_KEYS = frozenset({"model", "max_latency_ms", "flops", "confidential", "budget"})
BORROW_KEYS = frozenset({"market", "job", "fee", "member"})
VAULT_KEYS = frozenset()
DEPOSIT_KEYS = frozenset({"vault", "amount"})
REDEEM_KEYS = frozenset({"vault", "shares"})


class SchemaError(ValueError):
    pass


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    chars = bytearray()
    while n:
        n, rem = divmod(n, 58)
        chars.append(_B58[rem])
    pad = 0
    for byte in data:
        if byte == 0:
            pad += 1
        else:
            break
    return (bytes(( _B58[0],)) * pad + bytes(reversed(chars))).decode()


def b58decode(text: str) -> bytes:
    n = 0
    for char in text.encode():
        idx = _B58.find(char)
        if idx < 0:
            raise SchemaError("bad base58")
        n = n * 58 + idx
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for char in text:
        if char == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def did_from_public(public: bytes) -> str:
    if len(public) != 32:
        raise SchemaError("public key must be 32 bytes")
    return "did:key:z" + b58encode(_ED25519_PREFIX + public)


def public_from_did(did: str) -> bytes:
    if not _DID.match(did):
        raise SchemaError("from must be an ed25519 did:key")
    raw = b58decode(did.removeprefix("did:key:z"))
    if not raw.startswith(_ED25519_PREFIX) or len(raw) != 34:
        raise SchemaError("from must be an ed25519 did:key")
    return raw[2:]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def cid_for(unsigned: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical(unsigned).encode()).hexdigest()
    return "0x" + digest


def _amount(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _AMT.match(value):
        raise SchemaError(f"{field} must be a decimal integer string")
    return value


def _cid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _CID.match(value):
        raise SchemaError(f"{field} must be a 0x-prefixed sha256")
    return value


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise SchemaError(f"{field} must be a positive integer")
    return value


def _bps(value: Any, field: str, lo: int, hi: int) -> int:
    if type(value) is not int or isinstance(value, bool) or not lo <= value <= hi:
        raise SchemaError(f"{field} must be an integer from {lo} to {hi}")
    return value


def _did(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DID.match(value):
        raise SchemaError(f"{field} must be an ed25519 did:key")
    return value


def _positive_amount(value: Any, field: str) -> str:
    amount = _amount(value, field)
    if amount == "0":
        raise SchemaError(f"{field} must be positive")
    return amount


def validate_body(kind: str, body: Any) -> dict[str, Any]:
    if kind not in TYPES:
        raise SchemaError("unknown type")
    if not isinstance(body, dict):
        raise SchemaError("body must be an object")
    expected = {
        "offer": OFFER_KEYS,
        "accept": ACCEPT_KEYS,
        "member": MEMBER_KEYS,
        "receipt": RECEIPT_KEYS,
        "market": MARKET_KEYS,
        "supply": SUPPLY_KEYS,
        "withdraw": WITHDRAW_KEYS,
        "job": JOB_KEYS,
        "borrow": BORROW_KEYS,
        "vault": VAULT_KEYS,
        "deposit": DEPOSIT_KEYS,
        "redeem": REDEEM_KEYS,
    }[kind]
    if set(body) != expected:
        raise SchemaError(f"{kind} fields must be exactly {sorted(expected)}")
    if kind == "offer":
        _cid(body["model"], "model")
        for field in ("max_latency_ms", "flops", "draw_expiry", "repay_deadline"):
            if type(body[field]) is not int or isinstance(body[field], bool) or body[field] <= 0:
                raise SchemaError(f"{field} must be a positive integer")
        if type(body["confidential"]) is not bool:
            raise SchemaError("confidential must be a boolean")
        _amount(body["fee"], "fee")
        _amount(body["charge"], "charge")
        if body["fee"] == "0":
            raise SchemaError("fee must be positive")
        if body["repay_deadline"] < body["draw_expiry"]:
            raise SchemaError("repay_deadline is before draw_expiry")
    elif kind == "accept":
        _cid(body["offer"], "offer")
        _cid(body["job"], "job")
    elif kind == "member":
        _did(body["member"], "member")
        _positive_int(body["expiry"], "expiry")
    elif kind == "market":
        _cid(body["model"], "model")
        _positive_int(body["max_latency_ms"], "max_latency_ms")
        _positive_int(body["flops"], "flops")
        if type(body["confidential"]) is not bool:
            raise SchemaError("confidential must be a boolean")
        _bps(body["lltv_bps"], "lltv_bps", 1, 10_000)
        _bps(body["charge_bps"], "charge_bps", 0, 10_000)
        _positive_int(body["repay_window_ms"], "repay_window_ms")
        _positive_int(body["draw_window_ms"], "draw_window_ms")
        if type(body["guild"]) is not bool:
            raise SchemaError("guild must be a boolean")
        _did(body["curator"], "curator")
    elif kind == "supply":
        _cid(body["market"], "market")
        _positive_amount(body["amount"], "amount")
    elif kind == "withdraw":
        _cid(body["market"], "market")
        _positive_amount(body["shares"], "shares")
    elif kind == "job":
        _cid(body["model"], "model")
        _positive_int(body["max_latency_ms"], "max_latency_ms")
        _positive_int(body["flops"], "flops")
        if type(body["confidential"]) is not bool:
            raise SchemaError("confidential must be a boolean")
        _positive_amount(body["budget"], "budget")
    elif kind == "borrow":
        _cid(body["market"], "market")
        _cid(body["job"], "job")
        _positive_amount(body["fee"], "fee")
        _cid(body["member"], "member")
    elif kind == "vault":
        pass
    elif kind == "deposit":
        _cid(body["vault"], "vault")
        _positive_amount(body["amount"], "amount")
    elif kind == "redeem":
        _cid(body["vault"], "vault")
        _positive_amount(body["shares"], "shares")
    else:
        _cid(body["loan"], "loan")
        _cid(body["job"], "job")
        _cid(body["session"], "session")
        _amount(body["lender_amt"], "lender_amt")
        _amount(body["borrower_amt"], "borrower_amt")
    return body


def sign(kind: str, body: dict[str, Any], seed: bytes, ts: int, nonce: str) -> dict[str, Any]:
    if type(ts) is not int or isinstance(ts, bool) or ts <= 0:
        raise SchemaError("ts must be a positive integer")
    if not _NONCE.match(nonce):
        raise SchemaError("nonce must be 16 lowercase hex chars")
    validate_body(kind, body)
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes_raw()
    unsigned = {"t": kind, "from": did_from_public(public), "ts": ts, "n": nonce, "b": body}
    cid = cid_for(unsigned)
    sig = urlsafe_b64encode(private.sign(_SIG_DOMAIN + cid.encode())).decode().rstrip("=")
    return {**unsigned, "cid": cid, "sig": sig}


def verify(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_KEYS:
        raise SchemaError("envelope fields are wrong")
    kind = envelope["t"]
    if kind not in TYPES:
        raise SchemaError("unknown type")
    if type(envelope["ts"]) is not int or isinstance(envelope["ts"], bool) or envelope["ts"] <= 0:
        raise SchemaError("ts must be a positive integer")
    if not isinstance(envelope["n"], str) or not _NONCE.match(envelope["n"]):
        raise SchemaError("nonce must be 16 lowercase hex chars")
    did = envelope["from"]
    body = validate_body(kind, envelope["b"])
    unsigned = {"t": kind, "from": did, "ts": envelope["ts"], "n": envelope["n"], "b": body}
    cid = cid_for(unsigned)
    if envelope["cid"] != cid:
        raise SchemaError("cid does not match the body")
    sig_text = envelope["sig"]
    if not isinstance(sig_text, str) or sig_text == "" or "=" in sig_text:
        raise SchemaError("sig must be unpadded base64url")
    pad = "=" * ((4 - len(sig_text) % 4) % 4)
    try:
        sig = urlsafe_b64decode(sig_text + pad)
    except Exception as exc:
        raise SchemaError("sig must be unpadded base64url") from exc
    if len(sig) != 64:
        raise SchemaError("sig must be 64 bytes")
    public = Ed25519PublicKey.from_public_bytes(public_from_did(did))
    try:
        public.verify(sig, _SIG_DOMAIN + cid.encode())
    except Exception as exc:
        raise SchemaError("signature does not match from") from exc
    return envelope
