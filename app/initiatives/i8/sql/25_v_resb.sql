-- RESB -- reservation items. Where a repair ORDER shows up before any PR or PO.
--
-- A repair order is a reservation whose line for the faulty unit carries
-- movement type 543 (I8_REPAIR_RESERVATION_MOVEMENT_TYPE). Its BANFN/BNFPO
-- name the repair PR raised from it, and that is the key I08 follows: on the
-- 15-Oct-2026 extract 99.7% of the repair register traces back to a
-- reservation this way. The maintenance order (261/262) that took the unit off
-- the machine is a different reservation, with no key between the two.
--
-- Read for whether a repair PR is still open (phase 1, 27-Oct-2026): a PR
-- whose line here is deleted (XLOEK) or final-issued (KZEAR) is no longer a
-- repair anybody is waiting on. On the 15-Oct extract's 6,271 in-scope 543
-- lines on 80-series parts, 3,097 were deleted and 2,660 final-issued.
--
-- Nothing is filtered on here beyond scope: the movement type and both
-- indicators are settings, applied in Python where they can be unit-tested and
-- changed without a view rebuild.
--
-- AUFNR is exposed as "order" by the normalise layer -- a keyword everywhere --
-- so it is quoted, and kept out of the helpers (the staged inliner takes a bare
-- column only). The normalise layer has already stripped its leading zeros, so
-- an order-less line reads '' and becomes NULL here.
--
-- SCOPE: in-scope plants only -- see app/shared/plant_scope.py, which is the
-- one place the codes are written down. Filtered here rather than at render
-- time: a total computed over out-of-scope rows is wrong even when those rows
-- are never drawn. in_scope_plant() is generated from that module by views.py.
create or replace view v_resb as
select
    sap_key(reservation)                                 as rsnum,
    nullif(item_no_stock_transfer_reserv, '')            as rspos,
    sap_key(material)                                    as matnr,
    material                                             as matnr_raw,
    nullif(plant, '')                                    as werks,
    nullif(storage_location, '')                         as lgort,
    nullif(movement_type, '')                            as bwart,
    sap_key(purchase_requisition)                        as banfn,
    nullif(nullif(item_of_requisition, ''), '0')         as bnfpo,
    nullif("order", '')                                  as aufnr,
    nullif(item_deleted, '')                             as xloek,
    nullif(final_issue, '')                              as kzear,
    sap_date(requirement_date)                           as bdter,
    sap_num(requirement_quantity)                        as bdmng,
    nullif(base_unit_of_measure, '')                     as meins,
    sap_num(quantity_withdrawn)                          as enmng,
    nullif(text, '')                                     as sgtxt
from n_resb
where in_scope_plant(plant);
