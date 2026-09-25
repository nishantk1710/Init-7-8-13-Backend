# VZI CPI OData status, 2026-09-22T07:01:14Z

Run mode: --env-file .env --fields --out ./discovery

Calls: 2  |  failures: 0  |  total elapsed: 7s

## Services
- ZVZI_KPI02_SHARED_SRV: reachable, 14 entity sets in $metadata.
- ZMM_KPI02_SRV: reachable, 7 entity sets in $metadata.

## Field exposure
- 9 FRS-required field(s) absent from the projection; 0 newly exposed since the FRS; 0 regression(s).

## Slowest calls (W8.1 performance baseline)
- 6.7s  /sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/$metadata  
- 0.2s  /sap/opu/odata/sap/ZMM_KPI02_SRV/$metadata  

## Confirmed defect register

- **B1**: /$count returns HTTP 500 on PurchaseRequisitionSet and GoodsMovementItemSet
- **F1**: 85 of 230 properties silently ignore $filter (impossible-value test returns the set total)
- **F3**: only eq and substringof are honoured; ne, gt/ge/lt/le and startswith are not
- **F4**: $skip without $orderby produces duplicate and missing rows across pages
- **R1**: ReservationItemSet: ignored $filter property flagged to NTT as the priority fix
- **R2**: ReservationItemSet returns exactly 1,000 rows against 105,848 in the extract (suspected page cap)
