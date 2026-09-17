"""Policy version persistence.

Follows ``tests/test_db.py``: real Postgres or skip. SQLite would accept DDL and
types Postgres rejects, so passing against it would prove nothing about the
database actually used.

The round trip is the point. A recommendation stores ``(policy_id,
policy_version)`` and nothing else; if that pair cannot reconstruct the exact
rules months later, the recommendation is not auditable.
"""

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.contracts import PolicyStatus
from app.initiatives.i7.policy import PolicyDocument
from app.models import PolicyVersion

needs_db = pytest.mark.skipif(
    not get_settings().database_url, reason="DATABASE_URL not set"
)


@pytest.fixture
def session():
    factory = get_sessionmaker()
    with factory() as session:
        yield session
        session.rollback()


def _store(session, document: PolicyDocument) -> PolicyVersion:
    record = PolicyVersion(
        policy_id=document.policy_id,
        policy_version=document.policy_version,
        status=document.status.value,
        effective_from=document.effective_from,
        effective_to=document.effective_to,
        document_json=document.model_dump_json(),
    )
    session.add(record)
    session.flush()
    return record


@needs_db
def test_policy_document_round_trips_unchanged(session):
    """Serialise, store, reload, rebuild -- byte-identical rules."""
    original = PolicyDocument(policy_id="i07-test-roundtrip", policy_version=1)
    _store(session, original)

    stored = session.execute(
        select(PolicyVersion).where(PolicyVersion.policy_id == "i07-test-roundtrip")
    ).scalar_one()
    rebuilt = PolicyDocument.model_validate_json(stored.document_json)

    assert rebuilt == original
    assert rebuilt.classification.adi_cutoff == 1.32
    assert rebuilt.oar.confirmed is False


@needs_db
def test_unresolved_policies_survive_the_round_trip(session):
    """An unset service-level matrix must not deserialise into a default."""
    _store(session, PolicyDocument(policy_id="i07-test-unresolved", policy_version=1))

    stored = session.execute(
        select(PolicyVersion).where(PolicyVersion.policy_id == "i07-test-unresolved")
    ).scalar_one()
    rebuilt = PolicyDocument.model_validate_json(stored.document_json)

    assert rebuilt.service_level.is_configured is False
    assert rebuilt.max_stock.is_configured is False
    assert "service_level_matrix" in rebuilt.unresolved_policies()


@needs_db
def test_version_pair_is_unique(session):
    """Two rows for one version would make provenance ambiguous."""
    from sqlalchemy.exc import IntegrityError

    document = PolicyDocument(policy_id="i07-test-unique", policy_version=1)
    _store(session, document)
    with pytest.raises(IntegrityError):
        _store(session, document)


@needs_db
def test_versions_coexist_so_old_recommendations_stay_explainable(session):
    v1 = PolicyDocument(policy_id="i07-test-versions", policy_version=1)
    v2 = PolicyDocument(
        policy_id="i07-test-versions", policy_version=2, status=PolicyStatus.SIGNED
    )
    _store(session, v1)
    _store(session, v2)

    rows = session.execute(
        select(PolicyVersion)
        .where(PolicyVersion.policy_id == "i07-test-versions")
        .order_by(PolicyVersion.policy_version)
    ).scalars().all()

    assert [row.policy_version for row in rows] == [1, 2]
    assert [row.status for row in rows] == ["DRAFT", "SIGNED"]
