-- MSEG -- goods movement line items. Where dispatch to the vendor (541) and
-- the repaired unit coming back (101) actually show up.
--
-- Two things this view makes visible rather than hiding:
--
-- 1. A 541 posts TWO rows -- the vendor special-stock side (SOBKZ 'O',
--    SHKZG 'S') and the plant side (SOBKZ null, SHKZG 'H'). Measured at
--    exactly 355 of each on 80-series materials. Counting rows without
--    picking a side doubles every dispatch quantity.
--
-- 2. EBELP is NULL on a movement that carries no PO line. The raw column holds
--    '0' for those, and '0' is not a PO item number -- real ones are 10, 20,
--    30... So '0' is mapped to NULL here, where it reads as "this movement
--    cannot be attached to a line" instead of quietly joining to nothing.
--    132 of the 710 dispatches on 80-series materials are in that state.
create or replace view v_mseg as
select
    nullif(material_document, '')        as mblnr,
    nullif(material_doc_year, '')        as mjahr,
    nullif(material_doc_item, '')        as zeile,
    nullif(movement_type, '')            as bwart,
    sap_key(material)                    as matnr,
    material                             as matnr_raw,
    nullif(plant, '')                    as werks,
    nullif(storage_location, '')         as lgort,
    sap_date(posting_date)               as budat,
    sap_num(quantity)                    as menge,
    nullif(debit_credit_ind, '')         as shkzg,
    nullif(special_stock, '')            as sobkz,
    sap_key(purchase_order)              as ebeln,
    nullif(nullif(item, ''), '0')        as ebelp,
    sap_key(supplier)                    as lifnr
from raw_mseg;
