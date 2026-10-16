-- MBEW -- material valuation: what SAP values a material at, per plant.
--
-- Read by the New Item Price on the FR-2 coding screen
-- (app/initiatives/i8/new_item_price.py), which decides which row is the
-- price. This view only exposes the rows; it picks none of them.
--
-- BWKEY is the valuation area. Valuation is at plant level on this landscape,
-- so it IS the plant: 1300 and 1500 each carry their own rows.
--
-- One row per material + plant for an ordinary material, BWTAR blank. A
-- split-valuated material -- every 80-series one -- has a row per condition
-- (NEW, REFURBISHD, DAMAGED, SCRAP) AND a blank-BWTAR row that aggregates
-- them, so for those the blank row is not a price for any one condition.
--
-- VERPR is the moving average price, quoted per PEINH units (PEINH was 1 on
-- every in-scope row on 15-Oct-2026, but that is the extract's habit, not a
-- rule). R1.00 / R0.01 values are deliberate placeholder valuations -- see
-- I8_NEW_ITEM_PRICE_TOKEN_MAX.
--
-- SCOPE: in-scope plants only -- see app/shared/plant_scope.py.
create or replace view v_mbew as
select
    sap_key(material)              as matnr,
    material                       as matnr_raw,
    nullif(valuation_area, '')     as bwkey,
    nullif(valuation_type, '')     as bwtar,
    sap_num(moving_price)          as verpr,
    sap_num(price_unit)            as peinh
from n_mbew
where in_scope_plant(valuation_area);
