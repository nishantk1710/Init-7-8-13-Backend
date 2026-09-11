-- MARC -- material by plant: MRP type, reorder point, lead time.
--
-- PLANT 1300 (Black Mountain) and 1200 only. ZERO rows for Gamsberg (1500), so
-- reorderPoint and mrpType are NULL for every Gamsberg material -- a known gap
-- recorded in app/seed/manifest.py MISSING_FROM_DELIVERY, not a bug here.
create or replace view v_marc as
select
    sap_key(material)             as matnr,
    material                      as matnr_raw,
    nullif(plant, '')             as werks,
    nullif(mrp_type, '')          as dismm,
    sap_num(reorder_point)        as minbe,
    sap_num(planned_deliv_time)   as plifz,
    nullif(procurement_type, '')  as beskz
from raw_marc;
