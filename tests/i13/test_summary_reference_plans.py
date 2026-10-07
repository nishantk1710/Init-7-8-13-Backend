"""The summary says how many generated reference plans fed its counts.

The frontend used to hard-code "742" for this, which was only ever true where
I13_REFERENCE_PLANS_ENABLED was on. Served from the snapshot instead, it is 0
wherever the switch is off -- production included.
"""

from types import SimpleNamespace

import pytest

from app.initiatives.i13 import summary as summary_module
from app.initiatives.i13.models import AgingBand
from app.initiatives.i13.summary import summary_from_snapshot


def _snapshot(reference_plans: tuple) -> SimpleNamespace:
    return SimpleNamespace(
        oar_position_count=0,
        band_counts={band.value: 0 for band in AgingBand},
        reclassification=(),
        reference_plans=reference_plans,
    )


@pytest.fixture(autouse=True)
def no_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary_module, "exception_queue", lambda snapshot, plans: ())


def test_counts_the_reference_plans_the_snapshot_was_built_with() -> None:
    summary = summary_from_snapshot(_snapshot((object(), object(), object())), plans=[])

    assert summary.reference_plan_count == 3


def test_zero_when_the_snapshot_carries_none() -> None:
    """What every environment with the switch off serves."""
    summary = summary_from_snapshot(_snapshot(()), plans=[])

    assert summary.reference_plan_count == 0
