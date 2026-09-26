-- EKKO -- purchase order header. Document type (BSART) lives here.
--
-- Only 140 ZREP headers exist in this extract, against 1,225 item-category-3
-- lines: raw_ekko starts at 07-Jan-2025 while raw_ekpo reaches further back.
-- An INNER JOIN to this view drops 455 genuine repair lines (37% of the
-- register). Always LEFT JOIN it. See the task plan, section 7.4.
create or replace view v_ekko as
select
    sap_key(purchasing_document)     as ebeln,
    purchasing_document              as ebeln_raw,
    nullif(purchasing_doc_type, '')  as bsart,
    -- LIFNR. The extract labels this column "Supplier"; the column literally
    -- headed "Vendor" is empty in all 82,718 rows -- measured, not assumed.
    sap_key(supplier)                as lifnr,
    sap_date(document_date)          as bedat,
    sap_date(created_on)             as aedat,
    nullif(purchasing_group, '')     as ekgrp,
    nullif(company_code, '')         as bukrs,
    nullif(currency, '')             as waers
from raw_ekko;
