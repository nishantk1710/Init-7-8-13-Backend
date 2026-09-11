-- EKBE -- PO history: goods receipts, invoices and their reversals, per PO line.
--
-- SHKZG is the netting key. Measured on the repair lines: every 101 carries
-- 'S' (debit, a receipt) and every 102 carries 'H' (credit, its reversal).
-- Summing quantity without the sign counts a reversed receipt twice.
create or replace view v_ekbe as
select
    sap_key(purchasing_document)      as ebeln,
    purchasing_document               as ebeln_raw,
    nullif(item, '')                  as ebelp,
    nullif(po_history_category, '')   as bewtp,
    nullif(movement_type, '')         as bwart,
    sap_date(posting_date)            as budat,
    sap_num(quantity)                 as menge,
    nullif(debit_credit_ind, '')      as shkzg,
    sap_key(material)                 as matnr,
    nullif(plant, '')                 as werks,
    nullif(material_document, '')     as belnr,
    nullif(material_doc_year, '')     as gjahr
from raw_ekbe;
