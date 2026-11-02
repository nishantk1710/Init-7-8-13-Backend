-- MCHB -- stock per batch. On this SAP, stock per CONDITION.
--
-- 80-series parts are not batch-managed, yet SAP keeps one MCHB row per
-- condition in the batch field: NEW, REFURBISHD, DAMAGED (15-Oct-2026 extract:
-- 1,667 / 975 / 790 in-scope 80-series rows). CLABS -- unrestricted stock --
-- summed across a part's rows matches MARD's total on all 730 stocked
-- 80-series parts (1,427 units, measured 26-Oct-2026), so this is where usable
-- vs damaged stock is read from by default (I8_STOCK_CONDITION_SOURCE=MCHB).
--
-- One row PER STORAGE LOCATION AND CONDITION, like MARD per location: anything
-- reading stock must SUM. Which conditions count as usable or damaged is a
-- setting, applied in Python; this view picks none of them.
--
-- SCOPE: in-scope plants only -- see app/shared/plant_scope.py, which is the
-- one place the codes are written down. Filtered here rather than at render
-- time: a total computed over out-of-scope rows is wrong even when those rows
-- are never drawn. in_scope_plant() is generated from that module by views.py.
create or replace view v_mchb as
select
    sap_key(material)               as matnr,
    material                        as matnr_raw,
    nullif(plant, '')               as werks,
    nullif(storage_location, '')    as lgort,
    nullif(batch, '')               as charg,
    sap_num(unrestricted)           as clabs
from n_mchb
where in_scope_plant(plant);
