# Isolated markets

Status: implemented in `session_credit/market.py`. Bilateral `offer` / `accept` loans in `schema.py` stay valid and do not share liquidity with a market.

An isolated market is one pinned session and one loan-to-value. Lending agents supply FLOP to that market. A borrowing agent draws one session key from the market's idle FLOP. Repaid charges accrue to every supplier in that market. FLOP in one market cannot fund a session in another.

The borrower never receives a transferable balance. The session key follows the rules of `draw.pay`, `draw.transfer`, and `draw.expire`: one matching request, no transfer, unused fee returned.

There is no on-chain interest-rate model, price oracle, or liquidator. The checks below are pure functions over signed messages. A live FLOP settlement path is out of scope; the ledger under test is `fakechain`.

## 1. Market

Anyone may sign a `market`. Two markets with the same parameters and different cids do not merge.

| Body field | Rule |
|---|---|
| `model` | `0x` + 64 lowercase hex, the model-weight hash |
| `max_latency_ms` | positive integer |
| `flops` | positive integer, the only session size this market funds |
| `confidential` | boolean |
| `lltv_bps` | integer, `1`–`10000`. `10000` means the repayment may equal the job budget |
| `charge_bps` | integer, `0`–`10000`. Charge on a draw is `fee * charge_bps // 10000` |
| `repay_window_ms` | positive integer. `repay_deadline = borrow.ts + repay_window_ms` |
| `draw_window_ms` | positive integer. An unpaid session may be expired at `borrow.ts + draw_window_ms` |
| `guild` | boolean |
| `curator` | Ed25519 `did:key` |

When `guild` is false, `curator` is recorded and any DID may supply. When `guild` is true, only `curator` may supply, and a borrow must pass the member gate in §5.

Envelope rules are the ones in `schema.py`: types `market`, `supply`, `withdraw`, `job`, `borrow`; canonical JSON; cid over the unsigned envelope; Ed25519 signature over `session-credit-sig|` + cid; unknown fields rejected; amounts are decimal integer strings with no leading zeros.

## 2. Ledger inside one market

The market account on the ledger is the market cid. Its integers:

| Name | Meaning |
|---|---|
| `idle` | FLOP that can be withdrawn or drawn |
| `outstanding` | fees currently in session escrows |
| `total_shares` | claim units on `idle + outstanding` |

`total_assets = idle + outstanding`.

Invariants, checked after every successful message:

- `idle >= 0`, `outstanding >= 0`, `total_shares >= 0`
- `total_shares == 0` implies `total_assets == 0`
- the sum of supplier share balances equals `total_shares`
- a session escrow holds exactly one borrow's fee until `pay`, `expire`, or a refund

## 3. Supply and withdraw

`supply` body: `market` (cid), `amount` (positive decimal string). The signer must hold `amount` as liquid FLOP. That amount moves from the signer into `idle`.

Shares minted to the signer:

- if `total_shares == 0`, mint `amount` (the market must also have `total_assets == 0`; otherwise reject)
- otherwise mint `amount * total_shares // total_assets`
- a mint of 0 shares is rejected and the FLOP does not move

`withdraw` body: `market` (cid), `shares` (positive decimal string). The signer must hold at least that many shares.

Assets out = `shares * total_assets // total_shares`. Reject when that is 0 or when it is greater than `idle`. Subtract the assets from `idle`, burn the shares, and return the assets to the signer's liquid balance. Borrowed principal is not withdrawable.

Unborrowed supply earns nothing. Charges increase `total_assets` only when a receipt is applied (§6), so the exchange rate `total_assets / total_shares` moves only then.

A guild market rejects `supply` whose `from` is not `curator`.

## 4. Job, the collateral

The borrower does not declare the budget. The payer signs a `job`:

| Body field | Rule |
|---|---|
| `model`, `max_latency_ms`, `flops`, `confidential` | must equal the market on the borrow |
| `budget` | positive decimal string, the most the job will repay for this session |

A borrow that names a job with different session fields is rejected.

## 5. Borrow

`borrow` body: `market` (cid), `job` (cid), `fee` (positive decimal string), `member` (cid of a `member` message, or `0x` + 64 `0` digits when the market is not a guild).

Reject the borrow unless all of these hold at `borrow.ts`:

1. `market` and `job` verify, and the job's session fields equal the market.
2. `fee <= idle`.
3. `charge = fee * charge_bps // 10000`.
4. `(fee + charge) * 10000 <= budget * lltv_bps`.
5. For a guild market, `member` verifies, `member.from == curator`, `member.b.member == borrow.from`, and `borrow.ts < member.b.expiry`. For any other market, `member` must be the all-zero cid and the member gate is skipped.
6. No earlier `borrow` with this cid has already drawn.

On success, move `fee` from `idle` to `outstanding` and lock it in a session escrow whose id is the borrow cid. The pinned request is the market's `model`, `max_latency_ms`, `flops`, `confidential`, and this `fee`. `draw.pay` and `draw.transfer` apply to that escrow: a mismatched request or a transfer moves nothing. `draw.expire` may return the fee when `now >= borrow.ts + draw_window_ms` and the session has not been paid. `pay` at or after that time is rejected.

`repay_deadline = borrow.ts + repay_window_ms`. `draw_expiry` for `loan.reduce` is `borrow.ts + draw_window_ms`. The borrow takes the role of the accept. `session_settled`, both challenge windows, `fraud`, and `deadline` keep their current meanings.

## 6. Receipt and failure

The existing `receipt` is the repayment. `loan` is the borrow cid. `lender_amt` must equal `fee + charge`. `borrower_amt` is the remainder and may be `0`.

Apply a verified receipt only when the loan state allows `receipt` (state `session_final`). Then:

- `outstanding` decreases by `fee`
- `idle` increases by `fee + charge`
- supplier shares are unchanged, so the charge raises the exchange rate

An upheld fraud verdict (`loan.reduce` → `refunded`) returns `fee` from the session path to `idle`, decreases `outstanding` by `fee`, and adds no charge. A following receipt is rejected.

`expire` of an unpaid session returns `fee` to `idle` and decreases `outstanding` by `fee`.

`deadline` with no receipt (`defaulted`) leaves `outstanding` as a loss: subtract `fee` from `outstanding` and do not add it to `idle`. Shares stay, so `total_assets` falls and every supplier in that market takes the loss in proportion. No other market is touched.

Collateral claim on default is the signed job cid and, for a guild market, the member message. The spec records the claim. It does not move a second asset.

## 7. Curator vault

A vault is optional and does not change §§1–6.

The vault is an agent DID. Other agents sign `deposit` (`vault` cid, `amount`) and `redeem` (`vault` cid, `shares`). The vault signs `allocate` (`market` cid, `amount`), which is a `supply` whose `from` is the vault, and it signs `withdraw` on markets it supplied.

`redeem` pays out only from the vault's liquid balance, which is deposits minus amounts currently supplied to markets plus assets the vault has withdrawn. A redeem that would need to pull from a market is rejected until the vault signs that withdraw.

Guild markets are how a treasury lends: the treasury is `curator`, it is the only supplier, and every borrow names a live member message.

## 8. What an indexer stores

Per market cid: the market message, `idle`, `outstanding`, `total_shares`, and each supplier's share balance.

Per borrow cid: the borrow, the job, the session escrow, and the `loan.reduce` state.

A client replays these from the signed messages in cid order, using `ts` and then cid as the tie break. A message that fails a check in §§3–6 is dropped and does not change integers.

## 9. Acceptance tests

These are the exit tests for an implementation. All of them run on `FakeChain` with no network.

1. Two markets that differ only by `model` do not share `idle`. A borrow against one does not reduce the other.
2. First supplier of 100 receives 100 shares. A second supplier, after a charge has landed, receives strictly fewer shares than the amount supplied.
3. Withdraw of shares whose assets exceed `idle` is rejected and changes nothing. Withdraw of the idle portion returns that FLOP to the supplier.
4. A borrow with `fee + charge` above `budget * lltv_bps / 10000` is rejected. The boundary case that equals the limit is accepted.
5. The session key cannot transfer. A second `pay`, or a `pay` with a different model, is rejected. `expire` before `pay` restores `idle`.
6. A receipt for `fee + charge` increases `idle` by that sum and leaves share counts unchanged. A later supplier gets fewer shares per FLOP than the first.
7. `defaulted` reduces `total_assets` by `fee` in that market only.
8. `refunded` restores `fee` to `idle` and adds no charge.
9. A guild market rejects a supply from anyone but `curator`, and rejects a borrow with a missing, expired, or wrong-member message.
10. A vault redeem greater than the vault's liquid balance is rejected while the vault's market supply is still outstanding.
