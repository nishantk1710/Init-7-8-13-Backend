# VZI CPI OData status, 2026-09-22T05:28:25Z

Run mode: full sweep

Calls: 349  |  failures: 124  |  total elapsed: 160s

## Services
- ZVZI_KPI02_SHARED_SRV: reachable, 14 entity sets in $metadata.
- ZMM_KPI02_SRV: reachable, 7 entity sets in $metadata.

## Defects
- B1: /$count still fails on 5 set(s): ZMM_KPI02_SRV/MaterialValuationSet, ZMM_KPI02_SRV/ChangeDocHeaderSet, ZMM_KPI02_SRV/ChangeDocItemSet, ZMM_KPI02_SRV/BatchStockSet, ZMM_KPI02_SRV/MonthlyMovementStatisticSet
- R2 (ReservationItemSet volume): CAP CONFIRMED on $count: paging returned 7088 rows against a declared 1000. $count is not a usable total on this set.
- F4: MaterialPlantSet pulled without $orderby carries 0 duplicate key(s) - the defect appears FIXED.
- F3: operators honoured on MaterialPlantSet: none beyond eq.
- F1: 0 of 108 tested properties silently ignore $filter. Previously 85 of 230.
- R1 (ReservationItemSet priority fix): 0 ignored propert(ies) - appears FIXED.

## Field exposure
- 9 FRS-required field(s) absent from the projection; 0 newly exposed since the FRS; 0 regression(s).

## OAR scope
- DISMM in (ND, PD) = 1011 of 2183 material-plant positions; VB = 45; excluded = 1127. Reconciliation: UNEXPLAINED REMAINDER 26.

## Plant coverage
- Plant 1500 has no MARC rows over OData either - raise the DPC selection with NTT.
- Transacting plants with no MARC coverage: 1100, 1200, 1300, 1400, 1500, 1600, 2000, 3000, 3100, 3200, 3400, PDWB.

## Coverage
- MAKT covers 100.1% of MaterialSet over OData (extract showed 8.2%).

## I08 80-series
- 26 80-series materials visible on MaterialSet.

## Dictionary
- EKBE Vgabe distinct values over OData: none. Reconcile against the dictionary (1, 2) and I13 s7.1 (E).

## Slowest calls (W8.1 performance baseline)
- 3.4s  /sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/GoodsMovementItemSet  $top=1&$format=json
- 3.0s  /sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/GoodsMovementItemSet  $inlinecount=allpages&$top=1&$format=json
- 3.0s  /sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/GoodsMovementItemSet  $top=1000&$skip=0&$format=json
- 2.7s  /sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/PurchaseRequisitionSet  $inlinecount=allpages&$top=1&$format=json
- 2.6s  /sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/MaterialDocumentHeaderSet  $top=1000&$skip=40000&$format=json

## Confirmed defect register

- **B1**: /$count returns HTTP 500 on PurchaseRequisitionSet and GoodsMovementItemSet
- **F1**: 85 of 230 properties silently ignore $filter (impossible-value test returns the set total)
- **F3**: only eq and substringof are honoured; ne, gt/ge/lt/le and startswith are not
- **F4**: $skip without $orderby produces duplicate and missing rows across pages
- **R1**: ReservationItemSet: ignored $filter property flagged to NTT as the priority fix
- **R2**: ReservationItemSet returns exactly 1,000 rows against 105,848 in the extract (suspected page cap)
