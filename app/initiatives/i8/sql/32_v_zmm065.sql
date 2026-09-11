-- ZMM065 -- the VZI aging report. Carries the criticality tier the FRSs call
-- the platform-side critical parts list.
--
-- Unioned across both plants here. The two extracts have different shapes --
-- BMM 34 columns, Gamsberg 30 -- which is why the seed lands them as separate
-- tables; picking the four shared columns is what makes them one source.
-- Reading only the Black Mountain sheet would give criticality for 87 of the
-- 362 MARA 80-series materials and silently none for Gamsberg.
--
-- Criticality is the five-tier taxonomy W3.4 is built on: NORMAL, OBSOLETE,
-- CRITICAL, IMPACT, INSURANCE. Anything else, including blank, becomes NULL --
-- an unknown tier must never render as a real one.
create or replace view v_zmm065 as
select
    sap_key(mat_code)                as matnr,
    mat_code                         as matnr_raw,
    nullif(plant, '')                as werks,
    upper(nullif(btrim(criticality), '')) as criticality,
    nullif(material_description, '') as maktx,
    nullif(mattyp, '')               as mtart,
    nullif(mrp_type, '')             as dismm,
    'BMM'                            as source_report
from raw_zmm065_bmm
union all
select
    sap_key(mat_code),
    mat_code,
    nullif(plant, ''),
    upper(nullif(btrim(criticality), '')),
    nullif(material_description, ''),
    nullif(mattyp, ''),
    nullif(mrp_type, ''),
    'GB'
from raw_zmm065_gb;
