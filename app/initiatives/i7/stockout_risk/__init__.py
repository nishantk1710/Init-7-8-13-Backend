"""The High Stockout Risk calculation.

A material-plant is **at risk** when its stock on hand is below the stock it needs
to cover its supplier lead time::

    SOH               = unrestricted-use stock summed over storage locations (i7_staged_stock)
    ADC               = net quantity issued in the 90 days ending on the anchor date / 90
    lead time         = the resolved lead time in days in the feature store
                        (i7_material_feature.lead_time_days: the Initiative 11 program,
                        else MARC planned delivery time) -- not the recommendation's
    required stock    = ADC x lead time
    at risk           = SOH < required stock

Only **Fast** and **Slow** moving material-plants are judged, by the days between
the anchor date and the last goods issue, and every one of them is judged whether or
not a recommendation exists for it::

    0-365 days        Fast moving
    366-730 days      Slow moving
    over 730 / never  Non-moving    -- not part of this KPI

A Fast or Slow material-plant whose consumption, stock on hand or lead time is
missing or invalid is **Insufficient Data**, never "safe": the figure it would have
been judged on does not exist, and no missing value is read as zero. Invalid means a
lead time of zero or less (SAP's "not maintained"), a negative stock, a storage
location with no quantity, or a window or consumption that cannot be used. So is a
recommended material-plant in a plant whose movements were not loaded at all, because
"never issued" is only a fact where movements were loaded.

The percentage on the card is at-risk / evaluated, where evaluated is the
material-plants that could be judged (at risk plus not at risk).

The anchor date is the setting ``I7_STOCKOUT_RISK_AS_OF`` (see
``app.core.config``): the movement history is not current, so a window ending
today would be nearly empty. The per-material-plant usage lives in
``i7_stockout_risk_input``, built by ``python -m app.initiatives.i7.stockout_risk``.
"""

from datetime import date, timedelta

#: Days of consumption the average is taken over.
CONSUMPTION_WINDOW_DAYS = 90

#: Last issue within this many days of the anchor: Fast moving (0-365). The KPI judges Fast
#: and Slow together, so only the outer edge below is ever tested.
FAST_MAX_DAYS = 365

#: Last issue within this many days: Slow moving (366-730). Beyond it, or never: Non-moving,
#: which this KPI leaves out.
SLOW_MAX_DAYS = 730


def window_start(as_of: date, window_days: int = CONSUMPTION_WINDOW_DAYS) -> date:
    """First day of the consumption window: ``window_days`` days ending on, and including,
    ``as_of``. The default is ``CONSUMPTION_WINDOW_DAYS``; a saved KPI formula version can change
    it (see :mod:`app.initiatives.i7.kpi_formulas`)."""
    return as_of - timedelta(days=window_days - 1)
