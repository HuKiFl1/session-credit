"""Repayment state machine for one session-credit loan.

The caller passes `now` and the finality timestamps. This module does not
read a clock and does not call a chain.
"""

from __future__ import annotations

_TERMINAL = frozenset({"closed", "expired", "defaulted", "refunded"})


class LoanError(Exception):
    """The event is not a legal transition from this state."""


def reduce(
    state: str | None,
    event: str,
    now: int,
    *,
    draw_expiry: int,
    repay_deadline: int,
    session_final_at: int | None = None,
    receipt_final_at: int | None = None,
) -> str:
    """Apply one loan event and return the next state.

    Time guards that fail raise LoanError. `repaid` plus `deadline` stays
    `repaid`. A deadline during `session_settled` is not a transition.
    """
    if state in _TERMINAL:
        raise LoanError(f"{state} is terminal")

    if state is None:
        if event == "offer":
            return "offered"
        raise LoanError(f"no transition from {state} on {event}")

    if state == "offered":
        if event == "accept":
            if now >= draw_expiry:
                raise LoanError("accept at or after draw_expiry")
            return "accepted"
        if event == "expire":
            if now < draw_expiry:
                raise LoanError("expire before draw_expiry")
            return "expired"
        if event == "deadline":
            if now < repay_deadline:
                raise LoanError("deadline before repay_deadline")
            return "defaulted"
        raise LoanError(f"no transition from {state} on {event}")

    if state == "accepted":
        if event == "draw":
            return "drawn"
        if event == "deadline":
            if now < repay_deadline:
                raise LoanError("deadline before repay_deadline")
            return "defaulted"
        raise LoanError(f"no transition from {state} on {event}")

    if state == "drawn":
        if event == "expire":
            # Settlement is a state. `drawn` has not settled.
            if now < draw_expiry:
                raise LoanError("expire before draw_expiry")
            return "expired"
        if event == "session_settled":
            return "session_settled"
        if event == "deadline":
            if now < repay_deadline:
                raise LoanError("deadline before repay_deadline")
            return "defaulted"
        raise LoanError(f"no transition from {state} on {event}")

    if state == "session_settled":
        if event == "window":
            if session_final_at is None or now < session_final_at:
                raise LoanError("window before session_final_at")
            return "session_final"
        if event == "fraud":
            return "refunded"
        # Expire after settlement is illegal. Deadline does not default here;
        # it is not a listed event, so it raises rather than moving the loan.
        raise LoanError(f"no transition from {state} on {event}")

    if state == "session_final":
        if event == "fraud":
            return "refunded"
        if event == "receipt":
            return "repaid"
        if event == "deadline":
            if now < repay_deadline:
                raise LoanError("deadline before repay_deadline")
            return "defaulted"
        raise LoanError(f"no transition from {state} on {event}")

    if state == "repaid":
        if event == "window":
            if not _closed_reachable(now, session_final_at, receipt_final_at):
                raise LoanError("closed is unreachable")
            return "closed"
        if event == "deadline":
            return "repaid"
        raise LoanError(f"no transition from {state} on {event}")

    raise LoanError(f"no transition from {state} on {event}")


def _closed_reachable(
    now: int,
    session_final_at: int | None,
    receipt_final_at: int | None,
) -> bool:
    if session_final_at is None or receipt_final_at is None:
        return False
    return now >= session_final_at and now >= receipt_final_at
