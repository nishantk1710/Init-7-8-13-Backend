-- EKPO -- purchase order item. Item category (PSTYP) lives here.
--
-- PSTYP is deliberately exposed as a plain column and NOT filtered in this
-- view. The repair-PO predicate belongs in Python (ruling 5.2): SAP's OData
-- service accepts a $filter on Pstyp, answers HTTP 200 and ignores it, so the
-- rule has to live somewhere that survives cutover and can be unit-tested.
-- Baking '3' into a view would hard-code a convention that is still pending
-- SAP team confirmation.
create or replace view v_ekpo as
select
    sap_key(purchasing_document)       as ebeln,
    purchasing_document                as ebeln_raw,
    nullif(item, '')                   as ebelp,
    sap_key(material)                  as matnr,
    material                           as matnr_raw,
    nullif(plant, '')                  as werks,
    nullif(storage_location, '')       as lgort,
    nullif(item_category, '')          as pstyp,
    nullif(short_text, '')             as txz01,
    sap_num(order_quantity)            as menge,
    nullif(base_unit_of_measure, '')   as meins,
    sap_num(net_order_price)           as netpr,
    nullif(delivery_completed, '')     as elikz,
    nullif(purchase_requisition, '')   as banfn,
    nullif(item_of_requisition, '')    as bnfpo,
    nullif(deletion_indicator, '')     as loekz,
    nullif(material_type, '')          as mtart,
    -- ERDAT. The line's own creation date, and the reason the register can
    -- report daysOpen for every repair line: it is populated on all 1,225 of
    -- them, while EKKO -- the obvious source for a PO date -- has no header at
    -- all for 455. Per-line, so it is also more precise than a header date on
    -- a PO carrying several lines raised on different days.
    sap_date(creation_date)            as erdat,
    sap_date(last_changed_on)          as aedat,
    -- AFNAM. The requisitioner, and the only person-shaped field on a repair
    -- line: populated on all 1,225 of them, 27 distinct values. It is a code,
    -- not a name -- no person directory was delivered -- so it is served as a
    -- code, the same treatment an unnamed vendor gets. A code is a fact; an
    -- invented name is not. W5.3's declaration queue needs it for `requester`.
    --
    -- APPENDED AT THE END, and it has to stay that way. `create or replace
    -- view` can only add columns after the existing ones -- inserting one in
    -- the middle fails with "cannot change name of view column". A new column
    -- goes here, at the bottom, or the views stop being replaceable in place
    -- and every deploy needs a drop first.
    nullif(requisitioner, '')          as afnam
from raw_ekpo;
