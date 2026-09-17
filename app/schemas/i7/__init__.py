"""API-facing Pydantic schemas for Initiative 07.

Separate from the SQLAlchemy models in ``app.models.i7_recommendation`` on
purpose: a route must never return an ORM instance directly, both because that
leaks persistence detail (autoincrement ids, index-only columns) into the API
contract and because it would make a schema change and a migration the same
change, when they are not.

Every status/enum field here is a plain ``str`` typed against the *existing*
domain enums (``LifecycleStatus``, ``ApprovalRole``, ``AdoptionStatus``, ...),
never a new enum invented for the API. ``NOT_EVALUABLE``, ``UNKNOWN`` and
``NOT_CONFIGURED`` are real values a client must handle, not gaps to paper
over -- nothing here converts them to null or to an empty string.
"""
