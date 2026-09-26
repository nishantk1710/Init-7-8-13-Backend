"""Shared FastAPI dependencies for the I07 API.

Reuses ``app.core.db.get_db`` -- the same session dependency every other route
in the application uses. No second engine, no second session factory.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db as get_session
from app.models.i7_recommendation import Recommendation
from app.schemas.i7.errors import not_found

__all__ = ["get_session", "load_latest_recommendation"]


def load_latest_recommendation(session: Session, recommendation_id: str) -> Recommendation:
    """The most recently generated row for this recommendation_id.

    recommendation_id is a stable label, not a database-unique key (see
    app/models/i7_recommendation.py) -- a pipeline regeneration under new
    upstream run ids legitimately produces a second row. Every route resolves
    to the newest one, consistently.
    """
    row = session.execute(
        select(Recommendation)
        .where(Recommendation.recommendation_id == recommendation_id)
        .order_by(Recommendation.generated_at.desc(), Recommendation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise not_found(
            "RECOMMENDATION_NOT_FOUND",
            "Recommendation was not found.",
            recommendation_id=recommendation_id,
        )
    return row
