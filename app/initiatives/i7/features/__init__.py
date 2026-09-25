"""I07 feature store: demand statistics, classification, routing and OAR scope.

    i7_staged_*  ->  builder  ->  i7_material_feature  ->  Phase 4

* ``statistics``     ADI, CV-squared, and the dispersion measures (pure functions)
* ``classification`` history gate, the Syntetos-Boylan matrix, model routing
* ``oar_scope``      OAR evaluation at material-plant grain
* ``builder``        orchestration and persistence

Phase 3 computes *features and decisions*, not forecasts or inventory
parameters. Routing names a baseline and a challenger; it does not run them, and
no champion is recorded because champion selection needs Phase 4 backtesting.

Every threshold -- the gate, the ADI and CV-squared cutoffs, the confidence
bands -- comes from the Phase 1 policy document. None is written here.
"""

from app.initiatives.i7.features.builder import FeatureBuildResult, build_features
from app.initiatives.i7.features.classification import (
    BaselineModel,
    ChallengerModel,
    DataSufficiency,
    HistoryStatus,
    RoutingDecision,
    assess_history,
    classify_demand,
    route_models,
)
from app.initiatives.i7.features.oar_scope import OarAssessment, assess_oar_scope
from app.initiatives.i7.features.statistics import (
    DemandStatistics,
    Statistic,
    StatisticStatus,
    average_demand_interval,
    demand_statistics,
    squared_coefficient_of_variation,
)

__all__ = [
    "BaselineModel",
    "ChallengerModel",
    "DataSufficiency",
    "DemandStatistics",
    "FeatureBuildResult",
    "HistoryStatus",
    "OarAssessment",
    "RoutingDecision",
    "Statistic",
    "StatisticStatus",
    "assess_history",
    "assess_oar_scope",
    "average_demand_interval",
    "build_features",
    "classify_demand",
    "demand_statistics",
    "route_models",
    "squared_coefficient_of_variation",
]
