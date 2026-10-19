"""Smooth demand is sized by SAP, not by I07.

A material-plant classified SMOOTH is **not forecast**, gets **no calculated** ROP, safety
stock or max stock, and gets **no recommendation**: its current SAP values stand. I07's
forecasting, inventory and recommendation stages leave it out, and the Recommendations list
and summary do not show any SMOOTH row left over from before the rule.

Two places still need a SMOOTH material-plant's figures, and take them from SAP rather than
from a forecast:

* the OAR similar-materials engine, where a SMOOTH neighbour lends its current SAP ROP,
  safety stock and max stock -- as every neighbour now does, whatever its demand class
  (see ``oar.repository.load_inventory_values``); and
* the High Stockout Risk card, which counts SMOOTH material-plants from the feature store and
  its own usage table, never from a recommendation (see ``stockout_risk.counts``).

Every other demand class -- ERRATIC included -- is unchanged.

**An OAR material is the exception.** A material-plant ordered on demand (MRP
type ND / PD / blank) is sized from similar materials whatever its demand
class, so a SMOOTH OAR material *does* get an (OAR) recommendation. The rule
above applies to VB / other materials only.
"""

from app.initiatives.i7.contracts import DemandPattern

#: The ``demand_class`` value (``i7_material_feature`` / ``i7_recommendation``) of a material
#: that is not forecast and not recommended.
SMOOTH_DEMAND_CLASS = DemandPattern.SMOOTH.value
