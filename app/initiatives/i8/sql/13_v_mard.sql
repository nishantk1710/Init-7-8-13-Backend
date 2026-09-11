-- MARD -- stock by material + plant + storage location.
--
-- One row PER STORAGE LOCATION. A material sits in several bins, so anything
-- reading stock on hand must SUM across them; taking the first row
-- under-reports stock, which is the exact failure I08 exists to prevent.
--
-- 'unrestricted' is the current-period unrestricted stock (LABST). The
-- similarly-named 'unrestr_use_stock' column is the previous period -- checked
-- against the extract, not guessed.
create or replace view v_mard as
select
    sap_key(material)               as matnr,
    material                        as matnr_raw,
    nullif(plant, '')               as werks,
    nullif(storage_location, '')    as lgort,
    sap_num(unrestricted)           as labst,
    sap_num(in_quality_insp)        as insme,
    sap_num(blocked)                as speme
from raw_mard;
