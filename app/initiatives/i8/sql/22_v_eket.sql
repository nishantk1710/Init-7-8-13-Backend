-- EKET -- schedule lines. The source of the delivery date overdue is measured
-- against.
--
-- 63 of the 1,225 repair lines have NO row here at all -- not a blank date, no
-- schedule line. They are their own exception state (NO_DUE_DATE), never
-- "not overdue".
--
-- Measured: every repair item that does have schedule lines has exactly one.
-- The register still takes MIN(eindt) per item, so a second schedule line in a
-- later extract resolves to the earliest due date rather than an arbitrary one.
create or replace view v_eket as
select
    sap_key(purchasing_document)      as ebeln,
    purchasing_document               as ebeln_raw,
    nullif(item, '')                  as ebelp,
    nullif(schedule_line, '')         as etenr,
    sap_date(delivery_date)           as eindt,
    sap_num(scheduled_quantity)       as menge,
    sap_num(qty_delivered)            as wemng,
    nullif(purchase_requisition, '')  as banfn,
    nullif(item_of_requisition, '')   as bnfpo
from raw_eket;
