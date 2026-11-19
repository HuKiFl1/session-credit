"""Repayment transitions for one session-credit loan."""

from session_credit.loan import LoanError, reduce

_TERMS = dict(draw_expiry=1_000, repay_deadline=5_000, session_final_at=2_000, receipt_final_at=3_000)


def test_happy_path_closes_after_both_windows():
    state = reduce(None, "offer", 100, **_TERMS)
    assert state == "offered"
    state = reduce(state, "accept", 200, **_TERMS)
    assert state == "accepted"
    state = reduce(state, "draw", 300, **_TERMS)
    assert state == "drawn"
    state = reduce(state, "session_settled", 400, **_TERMS)
    assert state == "session_settled"
    state = reduce(state, "window", 2_000, **_TERMS)
    assert state == "session_final"
    state = reduce(state, "receipt", 2_100, **_TERMS)
    assert state == "repaid"
    state = reduce(state, "window", 3_000, **_TERMS)
    assert state == "closed"


def test_window_before_session_final_raises():
    raised = False
    try:
        reduce(
            "session_settled",
            "window",
            1_999,
            draw_expiry=1_000,
            repay_deadline=5_000,
            session_final_at=2_000,
            receipt_final_at=3_000,
        )
    except LoanError:
        raised = True
    assert raised


def test_fraud_from_session_settled_then_receipt_raises():
    state = reduce(
        "session_settled",
        "fraud",
        500,
        draw_expiry=1_000,
        repay_deadline=5_000,
        session_final_at=2_000,
        receipt_final_at=3_000,
    )
    assert state == "refunded"
    raised = False
    try:
        reduce(
            state,
            "receipt",
            600,
            draw_expiry=1_000,
            repay_deadline=5_000,
            session_final_at=2_000,
            receipt_final_at=3_000,
        )
    except LoanError:
        raised = True
    assert raised


def test_drawn_expire_at_draw_expiry():
    assert (
        reduce("drawn", "expire", 1_000, draw_expiry=1_000, repay_deadline=5_000)
        == "expired"
    )


def test_session_final_deadline_at_repay_deadline_defaults():
    assert (
        reduce(
            "session_final",
            "deadline",
            5_000,
            draw_expiry=1_000,
            repay_deadline=5_000,
            session_final_at=2_000,
            receipt_final_at=3_000,
        )
        == "defaulted"
    )


def test_window_into_closed_while_receipt_final_at_is_none_raises():
    raised = False
    try:
        reduce(
            "repaid",
            "window",
            3_000,
            draw_expiry=1_000,
            repay_deadline=5_000,
            session_final_at=2_000,
            receipt_final_at=None,
        )
    except LoanError:
        raised = True
    assert raised


def test_closed_unreachable_without_finality_or_before_either_timestamp():
    attempts = (
        dict(now=3_000, session_final_at=None, receipt_final_at=3_000),
        dict(now=3_000, session_final_at=2_000, receipt_final_at=None),
        dict(now=1_999, session_final_at=2_000, receipt_final_at=3_000),
        dict(now=2_999, session_final_at=2_000, receipt_final_at=3_000),
        dict(now=2_500, session_final_at=3_000, receipt_final_at=2_000),
    )
    for attempt in attempts:
        raised = False
        try:
            reduce(
                "repaid",
                "window",
                attempt["now"],
                draw_expiry=1_000,
                repay_deadline=5_000,
                session_final_at=attempt["session_final_at"],
                receipt_final_at=attempt["receipt_final_at"],
            )
        except LoanError:
            raised = True
        assert raised
