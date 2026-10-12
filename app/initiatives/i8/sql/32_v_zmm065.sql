-- ZMM065 -- the VZI criticality report, as the monthly uploads leave it.
-- Carries the criticality tier the FRSs call the platform-side critical parts
-- list, and the description, material type and MRP type the report states.
--
-- Read from criticality_current, not from the raw_zmm065_* workbook tables: that
-- is where each plant's monthly report is uploaded, the July delivery was
-- imported as its first one, and a material a newer report leaves out keeps the
-- tier it last had (app/integrations/criticality/reports.py). One row per
-- material-plant. Reading only the Black Mountain report would give criticality
-- for 87 of the 362 MARA 80-series materials and silently none for Gamsberg;
-- criticality_current holds both plants.
--
-- Criticality is the five-tier taxonomy W3.4 is built on: NORMAL, OBSOLETE,
-- CRITICAL, IMPACT, INSURANCE. A blank or unrecognised tier is NULL there --
-- an unknown tier must never render as a real one.
--
-- matnr is already stripped and trimmed (the same form sap_key() gives);
-- matnr_raw is what the file said.
--
-- SCOPE: in-scope plants only -- see app/shared/plant_scope.py, which is the
-- one place the codes are written down. Filtered here rather than at render
-- time: a total computed over out-of-scope rows is wrong even when those rows
-- are never drawn. in_scope_plant() is generated from that module by views.py.
create or replace view v_zmm065 as
select
    material                         as matnr,
    material_raw                     as matnr_raw,
    nullif(plant, '')                as werks,
    nullif(criticality, '')          as criticality,
    nullif(description, '')          as maktx,
    nullif(mattyp, '')               as mtart,
    nullif(mrp_type, '')             as dismm
from criticality_current
where in_scope_plant(plant);
