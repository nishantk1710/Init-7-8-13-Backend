-- EBAN -- purchase requisition item. The repair request BEFORE it becomes a
-- purchase order, and the second of the three free-text sources FR-2 asks the
-- coding screen to read (EKPO.TXZ01, EBAN.TXZ01, MSEG.SGTXT).
--
-- Two columns here do work no other view can do:
--
-- 1. EBELN/EBELP. A requisition that has been converted carries the PO it
--    became. The coding screen uses that to DROP the requisition line: the
--    same short text would otherwise be counted twice for one real request,
--    once as a PR and once as the PO it turned into. See
--    coding_candidates._dedupe_converted.
--
-- 2. LOEKZ. A deleted requisition line is not a request for anything. The
--    register already excludes deleted PO lines; this view exposes the same
--    flag so the screen can apply the same rule rather than a different one.
--
-- Like EKPO's PSTYP, nothing is filtered on here beyond scope: the deletion
-- rule and the conversion rule both belong in Python where they can be
-- unit-tested and changed without a view rebuild.
--
-- SCOPE: in-scope plants only -- see app/shared/plant_scope.py, which is the
-- one place the codes are written down. Filtered here rather than at render
-- time: a total computed over out-of-scope rows is wrong even when those rows
-- are never drawn. in_scope_plant() is generated from that module by views.py.
create or replace view v_eban as
select
    sap_key(purchase_requisition)      as banfn,
    nullif(item_of_requisition, '')    as bnfpo,
    sap_key(material)                  as matnr,
    material                           as matnr_raw,
    nullif(plant, '')                  as werks,
    nullif(storage_location, '')       as lgort,
    nullif(item_category, '')          as pstyp,
    nullif(short_text, '')             as txz01,
    sap_num(quantity_requested)        as menge,
-- No MEINS. EBAN's base unit is not one of the columns the extract
-- carries -- see the 'eban' vocabulary in app/shared/sap_normalise.py --
-- so selecting it here did not fail quietly, it stopped the whole view
-- from building. Nothing reads a unit off this view: the coding screen
-- wants the text, and the register takes its unit from EKPO.
    sap_date(requisition_date)         as badat,
    sap_date(delivery_date)            as lfdat,
    nullif(deletion_indicator, '')     as loekz,
    sap_key(purchase_order)            as ebeln,
    nullif(nullif(purchase_order_item, ''), '0') as ebelp,
    sap_key(vendor)                    as lifnr
from n_eban
where in_scope_plant(plant);
