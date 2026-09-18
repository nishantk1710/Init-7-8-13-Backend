"""W6.6 exception state machine: an explicit, validated transition table --
never an arbitrary status string assignment.

``RESOLVED`` -> ``OPEN``/``AWAITING_REQUESTER`` is never a listed transition:
per the W6.6 dedup rule (see ``service.py``), a resolved exception is not
reopened by a later detection run against the same business key -- only a
genuinely new occurrence (a new business key) creates a new exception.
"""

from __future__ import annotations

from app.initiatives.i13.act.domain import ExceptionStatus

ALLOWED_TRANSITIONS: dict[ExceptionStatus, frozenset[ExceptionStatus]] = {
    # A newly detected condition can be routed to its requester, or resolved
    # directly if the underlying condition clears before routing ever happens.
    ExceptionStatus.OPEN: frozenset({ExceptionStatus.AWAITING_REQUESTER, ExceptionStatus.RESOLVED}),
    # Routed to the requester: they can confirm, the response window can
    # elapse (escalate), or the condition can clear on its own before either.
    ExceptionStatus.AWAITING_REQUESTER: frozenset(
        {ExceptionStatus.CONFIRMED, ExceptionStatus.ESCALATED, ExceptionStatus.RESOLVED}
    ),
    # A confirmed/justified exception is closed by explicit review.
    ExceptionStatus.CONFIRMED: frozenset({ExceptionStatus.RESOLVED}),
    # An escalated exception is closed once the HOD (or the underlying
    # condition clearing) resolves it.
    ExceptionStatus.ESCALATED: frozenset({ExceptionStatus.RESOLVED}),
    # Terminal.
    ExceptionStatus.RESOLVED: frozenset(),
}


class InvalidTransitionError(ValueError):
    def __init__(self, from_status: ExceptionStatus, to_status: ExceptionStatus) -> None:
        super().__init__(f"cannot transition ACT exception from {from_status.value} to {to_status.value}")
        self.from_status = from_status
        self.to_status = to_status


def validate_transition(from_status: ExceptionStatus, to_status: ExceptionStatus) -> None:
    """Raises ``InvalidTransitionError`` unless ``to_status`` is a
    permitted next state for ``from_status``. Every status change in the
    ACT service goes through this -- there is no code path that assigns
    ``exception.status`` without calling it first."""
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
        raise InvalidTransitionError(from_status, to_status)
