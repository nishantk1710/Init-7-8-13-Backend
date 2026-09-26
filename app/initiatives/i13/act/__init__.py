"""W6.6 ACT layer: plan-breach/no-plan/quantity-override exception detection,
requester confirmation and HOD escalation.

Everything under this package is pure domain logic -- dataclasses, enums,
deterministic detection rules and the exception state machine -- and depends
only on the ports declared in ``ports.py``. Nothing here imports SQLAlchemy,
an Azure SDK, an SAP/CPI client or a notification-provider SDK; the concrete
adapters for today's environment (Postgres persistence, a logging
notification adapter, a config-driven HOD lookup) live alongside the rest of
Initiative 13 in ``app.initiatives.i13.act_exception_store``,
``app.initiatives.i13.act_notifications`` and
``app.initiatives.i13.act_hod_provider`` -- see those modules' docstrings for
what changes when Azure SQL, Entra/DOA and a production mail service are
integrated (the answer, by design, is "only those adapters").

W6.6 reuses W6.3's ``WatchMetricMart`` (GRNI evidence), W6.2's
``ReservationLedgerEntry``/``build_reservation_ledger`` and
``ConsumptionPlan``/``load_consumption_plans`` -- it never recomputes months
of cover, aging, GRNI or acquired-vs-plan.
"""
