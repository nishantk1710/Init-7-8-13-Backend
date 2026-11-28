"""The Min-Max -> OAR conversion requests as a queue: the newest request of each material-plant, across materials.

The Action Center and the Pipeline page need "which conversion requests are waiting, and on whom" for every material
at once; ``service.latest_request`` answers that for one. Read-only: nothing here changes a request or calls SAP.

One row per material-plant -- its newest request -- so a material that was rejected and asked for again appears once,
as the new request. Rows are ordered by when they last moved, newest first.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.i7_reverse import ReverseConversionRequest, ReverseRecommendation


@dataclass(frozen=True)
class QueueRow:
    request: ReverseConversionRequest
    description: str | None
    """The material's description from the recommendation the request was raised for."""


@dataclass(frozen=True)
class QueuePage:
    rows: list[QueueRow]
    total: int
    """Requests matching the status filter (all pages)."""
    counts: dict[str, int]
    """Newest requests per status, over every status -- not narrowed by the filter."""


def _newest_ids():
    return (
        select(func.max(ReverseConversionRequest.id).label("id"))
        .group_by(ReverseConversionRequest.sap_material_number, ReverseConversionRequest.sap_plant_code)
        .subquery()
    )


def list_requests(
    session: Session,
    *,
    statuses: list[str] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> QueuePage:
    """The newest request of each material-plant, optionally only those in ``statuses``, one page of them."""
    newest = _newest_ids()
    base = select(ReverseConversionRequest).join(newest, newest.c.id == ReverseConversionRequest.id)

    counts = {
        status: int(count)
        for status, count in session.execute(
            select(ReverseConversionRequest.status, func.count())
            .join(newest, newest.c.id == ReverseConversionRequest.id)
            .group_by(ReverseConversionRequest.status)
        )
    }

    narrowed = base.where(ReverseConversionRequest.status.in_(statuses)) if statuses else base
    total = session.execute(select(func.count()).select_from(narrowed.subquery())).scalar_one()
    requests = list(
        session.scalars(
            narrowed.order_by(ReverseConversionRequest.updated_at.desc(), ReverseConversionRequest.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )

    descriptions: dict[tuple[int, str, str], str | None] = {}
    run_ids = sorted({request.run_id for request in requests if request.run_id is not None})
    if run_ids:
        materials = sorted({request.sap_material_number for request in requests})
        for run_id, material, plant, description in session.execute(
            select(
                ReverseRecommendation.run_id,
                ReverseRecommendation.sap_material_number,
                ReverseRecommendation.sap_plant_code,
                ReverseRecommendation.description,
            ).where(ReverseRecommendation.run_id.in_(run_ids), ReverseRecommendation.sap_material_number.in_(materials))
        ):
            descriptions[(run_id, material, plant)] = description

    return QueuePage(
        rows=[
            QueueRow(
                request=request,
                description=descriptions.get((request.run_id, request.sap_material_number, request.sap_plant_code)),
            )
            for request in requests
        ],
        total=int(total),
        counts=counts,
    )
