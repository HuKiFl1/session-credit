# session-credit

Agent-to-agent credit for one pinned FLOP inference session.

A lending agent fronts the fee for a session with a fixed model hash, latency, FLOPs, and confidentiality flag. The borrowing agent never receives FLOP it can transfer. Repayment is the next job receipt: fee plus an agreed charge to the lender, any remainder to the borrower. A guild treasury is the same loan, with the member accepted first and the treasury paid first.

Settlement against a live FLOP chain is not wired yet. Draws run on an in-memory ledger (`fakechain`) so the cap can be tested offline.

## Run the tests

Python 3.11+, [uv](https://docs.astral.sh/uv/).

```sh
cd session-credit
uv run pytest
```

## Layout

| Module | Role |
|---|---|
| `session_credit/schema.py` | Canonical JSON, `did:key` signatures, offer / accept / member / receipt |
| `session_credit/book.py` | Open offers grouped by model, latency, and confidentiality |
| `session_credit/draw.py` | Escrow the fee, pay one matching session, or return the unused cap |
| `session_credit/fakechain.py` | In-memory balances for that draw |
| `session_credit/loan.py` | State machine from offer through both challenge windows |
| `session_credit/guild.py` | Member gate and treasury-first receipt check |

## Messages

Every message is an envelope signed by an Ed25519 `did:key`. `verify` rejects a bad signature, a tampered body, and any field that is not in the schema. Amounts are decimal integer strings.

| Type | Body |
|---|---|
| `offer` | `model`, `max_latency_ms`, `flops`, `confidential`, `fee`, `charge`, `draw_expiry`, `repay_deadline` |
| `accept` | `offer` (the offer cid), `job` |
| `member` | `member` (borrower DID), `expiry` |
| `receipt` | `loan` (the offer cid), `job`, `lender_amt`, `borrower_amt`, `session` |

`book.ingest` keeps a verified offer open until the first verified accept. A second accept for the same offer is dropped. Lines that fail verification are chatter.

`draw.issue` moves the offer fee from the lender into an escrow keyed by the offer cid. `draw.pay` sends that escrow to the miner when the request matches the offer field for field. `draw.transfer` always fails. `draw.expire` returns a remaining escrow to the lender.

`loan.reduce` is a pure function. The caller passes the clock. A loan reaches `closed` only after the session and the receipt have both passed their challenge windows. An upheld fraud verdict ends the loan as `refunded`.
