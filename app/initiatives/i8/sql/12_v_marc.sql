-- MARC -- material by plant: MRP type, reorder point, lead time.
--
-- IN SCOPE, THIS VIEW IS PLANT 1300 ONLY: the July extract has 45,352 MARC
-- rows for Black Mountain and ZERO for Gamsberg (1500). So reorderPoint and
-- mrpType are NULL for every Gamsberg material -- a known gap recorded in
-- app/seed/manifest.py MISSING_FROM_DELIVERY, not a bug here. A MARC extract
-- covering 1500 is what closes it.
--
-- SCOPE: in-scope plants only -- see app/shared/plant_scope.py, which is the
-- one place the codes are written down. Filtered here rather than at render
-- time: a total computed over out-of-scope rows is wrong even when those rows
-- are never drawn. in_scope_plant() is generated from that module by views.py.
create or replace view v_marc as
select
    sap_key(material)             as matnr,
    material                      as matnr_raw,
    nullif(plant, '')             as werks,
    nullif(mrp_type, '')          as dismm,
    sap_num(reorder_point)        as minbe,
    sap_num(planned_deliv_time)   as plifz,
    nullif(procurement_type, '')  as beskz
from raw_marc
where in_scope_plant(plant);
