-- MARA -- material master, cross-plant.
--
-- Truncated in this extract: it holds 8000005632..8000006059 while purchasing
-- references repair materials from 8000000007 up. 321 of the 371 materials on
-- repair PO lines have no row here at all. That is why the 80-series test is a
-- predicate on the number (app/initiatives/i8/material_number.py) and this view
-- is only ever LEFT JOINed for description and type -- never used as the gate.
create or replace view v_mara as
select
    sap_key(material)                as matnr,
    material                         as matnr_raw,
    nullif(material_type, '')        as mtart,
    nullif(material_group, '')       as matkl,
    nullif(base_unit_of_measure, '') as meins,
    nullif(material_description, '') as maktx,
    nullif(ext_material_group, '')   as extwg
from raw_mara;
