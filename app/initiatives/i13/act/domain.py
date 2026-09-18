"""W6.6 ACT domain model: exception records, their state machine's states,
audit events, requester confirmations and the small evidence snapshots the
detection rules consume.

Frozen dataclasses throughout, matching the rest of ``app.initiatives.i13``
-- a state change produces a new ``ActException`` (via ``dataclasses.
replace``) rather than mutating one in place, so every transition is a value
the caller can hand to a repository/audit trail rather than a hidden
side-effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class ExceptionType(str, Enum):
    PLAN_BREACH = "PLAN_BREACH"
    NO_PLAN = "NO_PLAN"
    # No valid consumption plan/session AND W6.3's GRNI evidence (reused, not
    # recomputed) shows the received stock has sat unissued past its
    # configured threshold -- the FRS's "30-day GRNI fallback" for materials
    # that also have no plan to check a breach against.
    NO_PLAN_GRNI = "NO_PLAN_GRNI"
    QUANTITY_OVERRIDE = "QUANTITY_OVERRIDE"


class ExceptionStatus(str, Enum):
    OPEN = "OPEN"
    AWAITING_REQUESTER = "AWAITING_REQUESTER"
    CONFIRMED = "CONFIRMED"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"


class NoPlanReason(str, Enum):
    """Why a reservation is treated as having no valid plan/session, given
    today's CAPTURE contract (``app.initiatives.i13.plans.ConsumptionPlan``,
    where ``session_id`` is a field *on* the plan record, not a separate
    session store). See ``detection.classify_no_plan_reason`` for exactly
    when each value is produced.

    ``SESSION_WITHOUT_PLAN`` is kept for forward compatibility with a future
    CAPTURE source that tracks sessions independently of plans (e.g. a
    reservation-time chatbot session store) -- it cannot be produced from
    today's data, where a session_id only ever exists as a plan attribute, so
    "a session with no plan" is not a representable state yet. Not fabricated
    to force a match.
    """

    MISSING_SESSION = "MISSING_SESSION"
    INVALID_SESSION = "INVALID_SESSION"
    SESSION_WITHOUT_PLAN = "SESSION_WITHOUT_PLAN"


class RoutingStatus(str, Enum):
    """Whether an exception's current assignee (requester or HOD) was
    actually resolved to a real identity. Distinct from ``ExceptionStatus``:
    an exception can be ``ESCALATED`` (the workflow milestone) while routing
    is still ``PENDING`` (nobody could be identified yet) -- see
    ``service.process_escalations``."""

    RESOLVED = "RESOLVED"
    PENDING = "ROUTING_PENDING"
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class AssigneeType(str, Enum):
    REQUESTER = "REQUESTER"
    HOD = "HOD"


class EventType(str, Enum):
    DETECTED = "DETECTED"
    ROUTED_TO_REQUESTER = "ROUTED_TO_REQUESTER"
    REQUESTER_CONFIRMED = "REQUESTER_CONFIRMED"
    JUSTIFICATION_ADDED = "JUSTIFICATION_ADDED"
    ESCALATED_TO_HOD = "ESCALATED_TO_HOD"
    ROUTING_FAILED = "ROUTING_FAILED"
    RESOLVED = "RESOLVED"
    NOTIFICATION_SENT = "NOTIFICATION_SENT"
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED"


class NotificationChannel(str, Enum):
    PLATFORM_QUEUE = "PLATFORM_QUEUE"
    EMAIL = "EMAIL"


class NotificationOutcome(str, Enum):
    SENT = "SENT"
    # Recorded by today's dev/logging adapter -- the intent was captured for
    # audit, but no real delivery happened. Never reported as SENT, so a
    # reader can't mistake "logged" for "delivered".
    LOGGED_ONLY = "LOGGED_ONLY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ActException:
    """One accountable ACT exception. ``exception_id`` is the deterministic
    business key (see ``detection.build_exception_id``) -- the same
    unresolved condition always maps to the same id, which is what makes
    ``ExceptionRepository.upsert`` idempotent across repeated detection
    runs (see ``service.detect_exceptions``)."""

    exception_id: str
    exception_type: ExceptionType
    status: ExceptionStatus

    material: str
    plant: str

    reservation_number: str | None
    reservation_item: str | None
    session_id: str | None
    ledger_entry_id: str | None

    owner_requester_id: str | None

    detected_at: datetime
    requester_due_at: datetime | None
    escalated_at: datetime | None
    resolved_at: datetime | None

    current_assignee_type: AssigneeType | None
    current_assignee_id: str | None
    routing_status: RoutingStatus | None

    reason: str
    evidence: dict[str, str] = field(default_factory=dict)

    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ExceptionEvent:
    """Append-only audit trail entry. Never mutated or deleted -- see
    ``ExceptionRepository.append_event``."""

    exception_id: str
    event_type: EventType
    from_status: ExceptionStatus | None
    to_status: ExceptionStatus | None
    actor_id: str | None
    actor_type: str
    timestamp: datetime
    metadata: dict[str, str] = field(default_factory=dict)
    event_id: str | None = None


@dataclass(frozen=True)
class RequesterConfirmation:
    """Structured requester confirmation/justification (current v1.1 FRS:
    confirmation + reason category + free text -- not the older
    Confirm/Replan/Release action set)."""

    exception_id: str
    reason_category: str
    free_text: str
    actor_id: str
    submitted_at: datetime
    confirmation_id: str | None = None


@dataclass(frozen=True)
class NotificationIntent:
    exception_id: str
    channel: NotificationChannel
    recipient: str | None
    subject: str
    body: str


@dataclass(frozen=True)
class NotificationResult:
    outcome: NotificationOutcome
    detail: str


@dataclass(frozen=True)
class NotificationAttempt:
    """Persisted record of one notification attempt -- audit trail, kept
    even when delivery failed (see ``ExceptionRepository.record_notification``)."""

    exception_id: str
    channel: NotificationChannel
    recipient: str | None
    outcome: NotificationOutcome
    detail: str
    attempted_at: datetime
    notification_id: str | None = None


@dataclass(frozen=True)
class WatchGrniSnapshot:
    """The one slice of W6.3's ``WatchMetricMart`` the NO_PLAN_GRNI rule
    needs -- reused, never recomputed. Built by the persistence adapter
    (``act_exception_store.py``) from the mart row so this domain module
    never imports the ORM model directly."""

    material: str
    plant: str
    gr_not_issued_flag: bool
    gr_not_issued_days_since_gr: int | None
    gr_not_issued_threshold_days: int


@dataclass(frozen=True)
class QuantityDecisionRecord:
    """One reservation-time quantity decision to evaluate for an override.
    ``suggested_quantity`` is ``None`` whenever no quantity-suggestion source
    is wired in (true for every caller today -- the suggestion engine is
    W7.4, out of scope for W6.6) -- see ``detection.detect_quantity_override``."""

    material: str
    plant: str
    reservation_number: str | None
    reservation_item: str | None
    session_id: str | None
    requester_id: str | None
    requested_quantity: Decimal
    suggested_quantity: Decimal | None
    suggestion_reason: str | None = None
    override_justification: str | None = None


@dataclass(frozen=True)
class QuantityOverrideEvaluation:
    available: bool
    """``False`` means no suggestion source answered -- SOURCE_UNAVAILABLE,
    never fabricated as "no override"."""
    override: bool
    variance: Decimal | None


@dataclass(frozen=True)
class CrossPlantStockInfo:
    """Informational-only cross-plant stock context attached to an
    exception (FRS: cross-plant visibility, never a transfer/redeployment
    action)."""

    material: str
    plant: str
    stock_on_hand: Decimal
