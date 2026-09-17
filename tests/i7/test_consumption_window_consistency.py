"""Feature store and forecasting must agree on the consumption observation window.

Regression coverage for the fix that made
:func:`app.initiatives.i7.adapters.repository.consumption_series_for` (read by
forecasting) share the feature builder's extract-wide window and
densification (:func:`app.initiatives.i7.features.builder._densify`,
:func:`app.initiatives.i7.features.builder.observation_window`) instead of
densifying independently over each material's own first-to-last movement.

Real Postgres or skip. These assert on real staged data, not fixtures,
because the divergence this closes (471 material-plants) only shows up at
real volume -- see the feature builder's own docstring for the measured ADI
shift (1.500 -> 1.625).
"""

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.adapters import consumption_series_for
from app.initiatives.i7.features.builder import observation_window
from app.models.i7_features import MaterialFeature

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _built(session) -> bool:
    return session.execute(select(func.count()).select_from(MaterialFeature)).scalar() > 0


def _late_starting_material_plant(session):
    """A real material-plant whose first RAW movement is strictly after the
    extract's window start -- the exact case the old per-material window
    handled differently from the feature store.
    """
    window = observation_window(session)
    if window is None:
        return None
    start, _ = window
    row = session.execute(
        text(
            """
            select c.sap_material_number, c.sap_plant_code, min(c.period) as first_move
              from i7_staged_consumption c
              join i7_staged_material_plant p
                on p.sap_material_number = c.sap_material_number
               and p.sap_plant_code = c.sap_plant_code
             group by 1, 2
            having min(c.period) > :start
             order by min(c.period) desc
             limit 1
            """
        ),
        {"start": start},
    ).first()
    return row


# --- The core consistency property --------------------------------------------


@needs_db
def test_forecasting_series_length_matches_feature_store_total_periods(session):
    """total_periods must be identical whichever component reads the series."""
    if not _built(session):
        pytest.skip("features not built")
    candidate = _late_starting_material_plant(session)
    if candidate is None:
        pytest.skip("no late-starting material-plant in this extract")

    series = consumption_series_for(
        session, candidate.sap_material_number, candidate.sap_plant_code
    )
    feature = session.execute(
        select(MaterialFeature).where(
            MaterialFeature.sap_material_number == candidate.sap_material_number,
            MaterialFeature.sap_plant_code == candidate.sap_plant_code,
        )
    ).scalar_one_or_none()
    if feature is None:
        pytest.skip("candidate has no feature-store row")

    assert series is not None
    assert series.total_periods == feature.total_periods


@needs_db
def test_forecasting_non_zero_count_matches_feature_store(session):
    """non_zero_periods must also agree -- ADI's denominator, not just n."""
    if not _built(session):
        pytest.skip("features not built")
    candidate = _late_starting_material_plant(session)
    if candidate is None:
        pytest.skip("no late-starting material-plant in this extract")

    series = consumption_series_for(
        session, candidate.sap_material_number, candidate.sap_plant_code
    )
    feature = session.execute(
        select(MaterialFeature).where(
            MaterialFeature.sap_material_number == candidate.sap_material_number,
            MaterialFeature.sap_plant_code == candidate.sap_plant_code,
        )
    ).scalar_one_or_none()
    if feature is None:
        pytest.skip("candidate has no feature-store row")

    assert series is not None
    assert series.non_zero_periods == feature.non_zero_periods


@needs_db
def test_late_starting_series_spans_the_full_extract_window_not_its_own_span(session):
    """The regression itself: a late first movement must not shrink total_periods
    to the material's own first-to-last span. Before the fix, a material whose
    only movement fell in the extract's last month would have total_periods=1;
    after the fix it spans the whole window.
    """
    candidate = _late_starting_material_plant(session)
    if candidate is None:
        pytest.skip("no late-starting material-plant in this extract")

    window = observation_window(session)
    expected_total_periods = (
        (window[1].year - window[0].year) * 12 + (window[1].month - window[0].month) + 1
    )

    series = consumption_series_for(
        session, candidate.sap_material_number, candidate.sap_plant_code
    )
    assert series is not None
    assert series.total_periods == expected_total_periods
    assert series.observations[0].period == window[0]
    assert series.observations[-1].period == window[1]
    # The material's own first movement is strictly after the window start --
    # proving the series was NOT limited to first-to-last-movement.
    assert candidate.first_move > window[0]


# --- ADI agreement --------------------------------------------------------


@needs_db
def test_adi_from_forecasting_series_matches_stored_adi(session):
    """ADI computed from forecasting's own series must equal the stored value,
    wherever the feature-store row has one (SUFFICIENT history, non-zero
    demand exists). This is the whole point of the consistency fix: ADI counts
    total_periods / non_zero_periods, and both must now come from the same
    window regardless of which component computes them.
    """
    if not _built(session):
        pytest.skip("features not built")
    from app.initiatives.i7.features.statistics import average_demand_interval

    row = session.execute(
        select(MaterialFeature).where(MaterialFeature.adi.isnot(None)).limit(5)
    ).scalars().all()
    if not row:
        pytest.skip("no material-plant has a stored ADI")

    checked = 0
    for feature in row:
        series = consumption_series_for(
            session, feature.sap_material_number, feature.sap_plant_code
        )
        if series is None:
            continue
        recomputed = average_demand_interval(series.total_periods, series.non_zero_periods)
        # MaterialFeature.adi is Numeric(18, 6); the in-memory Statistic keeps
        # full Decimal division precision. Round to the column's own scale
        # before comparing -- a 6th-decimal-place difference is storage
        # rounding, not a disagreement between the two components' inputs.
        assert round(recomputed.value, 6) == feature.adi
        checked += 1
    if checked == 0:
        pytest.skip("none of the sampled rows had a matching consumption series")


# --- Edge cases ----------------------------------------------------------------


@needs_db
def test_no_history_returns_none(session):
    """A material-plant with no staged consumption rows at all: no series."""
    series = consumption_series_for(session, "___NEVER_STAGED___", "____")
    assert series is None


@needs_db
def test_gaps_between_movements_are_filled_with_zero(session):
    """Same behaviour the feature store already guarantees: a month between
    two movements with nothing staged is a real zero-demand month, not a gap.
    """
    window = observation_window(session)
    if window is None:
        pytest.skip("no consumption staged at all")

    row = session.execute(
        text(
            """
            select sap_material_number, sap_plant_code
              from i7_staged_consumption
             group by 1, 2
            having count(*) > 1
               and count(*) < (
                     extract(year from max(period)) * 12 + extract(month from max(period))
                   ) - (
                     extract(year from min(period)) * 12 + extract(month from min(period))
                   ) + 1
             limit 1
            """
        )
    ).first()
    if row is None:
        pytest.skip("no sparse series in the extract")

    series = consumption_series_for(session, row[0], row[1])
    assert series is not None
    assert series.total_periods > series.non_zero_periods
    assert any(observation.is_zero_demand for observation in series.observations)
    # Contiguous months, no gaps in the period sequence itself.
    periods = [observation.period for observation in series.observations]
    for earlier, later in zip(periods, periods[1:]):
        assert (later.year - earlier.year) * 12 + (later.month - earlier.month) == 1
