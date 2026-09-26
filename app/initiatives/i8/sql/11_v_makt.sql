-- MAKT -- material descriptions by language.
create or replace view v_makt as
select
    sap_key(material)                as matnr,
    material                         as matnr_raw,
    nullif(language_key, '')         as spras,
    nullif(material_description, '') as maktx
from raw_makt;
