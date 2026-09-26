import pytest

from app.initiatives.i13.act.domain import ExceptionStatus
from app.initiatives.i13.act.state_machine import InvalidTransitionError, validate_transition


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (ExceptionStatus.OPEN, ExceptionStatus.AWAITING_REQUESTER),
        (ExceptionStatus.OPEN, ExceptionStatus.RESOLVED),
        (ExceptionStatus.AWAITING_REQUESTER, ExceptionStatus.CONFIRMED),
        (ExceptionStatus.AWAITING_REQUESTER, ExceptionStatus.ESCALATED),
        (ExceptionStatus.AWAITING_REQUESTER, ExceptionStatus.RESOLVED),
        (ExceptionStatus.CONFIRMED, ExceptionStatus.RESOLVED),
        (ExceptionStatus.ESCALATED, ExceptionStatus.RESOLVED),
    ],
)
def test_allowed_transitions_do_not_raise(from_status: ExceptionStatus, to_status: ExceptionStatus) -> None:
    validate_transition(from_status, to_status)


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (ExceptionStatus.OPEN, ExceptionStatus.CONFIRMED),
        (ExceptionStatus.OPEN, ExceptionStatus.ESCALATED),
        (ExceptionStatus.AWAITING_REQUESTER, ExceptionStatus.OPEN),
        (ExceptionStatus.CONFIRMED, ExceptionStatus.AWAITING_REQUESTER),
        (ExceptionStatus.CONFIRMED, ExceptionStatus.ESCALATED),
        (ExceptionStatus.ESCALATED, ExceptionStatus.AWAITING_REQUESTER),
        (ExceptionStatus.ESCALATED, ExceptionStatus.CONFIRMED),
        (ExceptionStatus.RESOLVED, ExceptionStatus.OPEN),
        (ExceptionStatus.RESOLVED, ExceptionStatus.AWAITING_REQUESTER),
    ],
)
def test_disallowed_transitions_raise(from_status: ExceptionStatus, to_status: ExceptionStatus) -> None:
    with pytest.raises(InvalidTransitionError):
        validate_transition(from_status, to_status)
