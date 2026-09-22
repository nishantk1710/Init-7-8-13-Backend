"""W6.6 ports: the interfaces ACT application logic depends on instead of
any concrete infrastructure. Every one of these has exactly one adapter
today (Postgres/local-config/logging) and is designed to gain a second
(Azure SQL/Entra-DOA/production mail) later without this package or
``service.py`` changing -- see each adapter module's docstring for what
"later" looks like.
"""

from __future__ import annotations

from typing import Protocol

from app.initiatives.i13.act.domain import (
    ActException,
    CrossPlantStockInfo,
    ExceptionEvent,
    ExceptionStatus,
    ExceptionType,
    NotificationAttempt,
    NotificationIntent,
    NotificationResult,
    RequesterConfirmation,
)


class ExceptionRepository(Protocol):
    """Persistence for ACT exceptions, their audit trail and requester
    confirmations. The one adapter today is
    ``app.initiatives.i13.act_exception_store.SqlExceptionRepository``
    (Postgres/Azure SQL via SQLAlchemy) -- this Protocol carries no
    SQLAlchemy type anywhere in its signature so the ACT application layer
    never needs to import it."""

    def get(self, exception_id: str) -> ActException | None: ...

    def upsert(self, exception: ActException) -> None:
        """Insert a new exception or persist a state change to an existing
        one, keyed by ``exception.exception_id``. Callers are responsible for
        only ever moving through valid transitions (see
        ``state_machine.validate_transition``) -- this method does not
        re-validate them."""
        ...

    def list(
        self,
        *,
        material: str | None = None,
        plant: str | None = None,
        exception_type: ExceptionType | None = None,
        status: ExceptionStatus | None = None,
        owner_requester_id: str | None = None,
    ) -> list[ActException]: ...

    def append_event(self, event: ExceptionEvent) -> None: ...

    def list_events(self, exception_id: str) -> list[ExceptionEvent]: ...

    def save_confirmation(self, confirmation: RequesterConfirmation) -> None: ...

    def get_confirmation(self, exception_id: str) -> RequesterConfirmation | None: ...

    def record_notification(self, attempt: NotificationAttempt) -> None: ...


class EscalationRecipientProvider(Protocol):
    """Resolves who an exception escalates to. TODAY: a local/config
    mapping (``app.initiatives.i13.act_hod_provider.
    ConfigEscalationRecipientProvider``) or unresolved
    (``NullEscalationRecipientProvider``). LATER: an Entra/DOA-backed
    adapter implementing this same method. ACT business logic must not know
    which one is wired in.

    Returns ``None`` when no HOD can be identified -- callers must persist
    an explicit routing-pending state, never invent a recipient (see
    ``service.process_escalations``)."""

    def get_hod(self, *, material: str, plant: str, requester_id: str | None) -> str | None: ...


class NotificationPort(Protocol):
    """The locked W6.6 notification channels: platform exception queue and
    email. TODAY: ``app.initiatives.i13.act_notifications.
    LoggingNotificationAdapter`` (records intent, never claims delivery).
    LATER: a production mail adapter (SMTP/Graph/Azure Communication
    Services) implementing the same method -- ACT business logic must not
    import any of those SDKs directly."""

    def send(self, intent: NotificationIntent) -> NotificationResult: ...


class QuantitySuggestionProvider(Protocol):
    """Reservation-time quantity-suggestion source, for a caller that has a
    material/plant (or reservation) and wants the latest suggested figure for
    it.

    W7.4 built the engine itself (``app.initiatives.i13.quantity_suggestion``)
    and W6.6's detection is fed from its persisted rows through
    ``quantity_suggestion_store.build_quantity_decision_records``, which is a
    batch read rather than this per-key lookup -- so this port has no
    implementation today and is not on the detection path. Whatever
    implements it later must return ``None`` where nothing was suggested:
    SOURCE_UNAVAILABLE, never a fabricated quantity."""

    def get_suggested_quantity(
        self, *, material: str, plant: str, reservation_number: str | None, reservation_item: str | None
    ) -> object | None: ...  # Decimal | None; typed loosely to avoid a hard Decimal import cycle here


class CrossPlantStockProvider(Protocol):
    """Informational-only cross-plant stock lookup for one material, scoped
    away from the exception's own plant. TODAY: backed by the same Postgres
    stock read every WATCH metric uses. Never used for transfer/reservation
    creation -- see the module docstring on ``service.py``."""

    def get_other_plant_stock(self, *, material: str, exclude_plant: str) -> list[CrossPlantStockInfo]: ...
