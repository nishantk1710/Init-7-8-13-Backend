# VZI CPI OData status, 2026-09-25T18:11:15Z

Run mode: full sweep

Calls: 507  |  failures: 133  |  total elapsed: 261s

## Services
- ZMM_KPI02_ADD_SRV: reachable, 14 entity sets in $metadata.
- ZMM_KPI02_TAB_SRV: reachable, 7 entity sets in $metadata.

## Defects
- B1: /$count succeeded on every reachable set - the defect appears FIXED.
- R2 (ReservationItemSet volume): $count and paging agree at 7088 rows.
- F4: MaterialPlantSet pulled without $orderby carries 0 duplicate key(s) - the defect appears FIXED.
- F3: operators honoured on MaterialPlantSet: ne, ge/le range on string, gt on string, or on honoured property, startswith, substringof, and across two honoured properties.
- F1: 2 of 146 tested properties silently ignore $filter. Previously 85 of 230.
- R1 (ReservationItemSet priority fix): 0 ignored propert(ies) - appears FIXED.

## Field exposure
- 3 FRS-required field(s) absent from the projection; 6 newly exposed since the FRS; 0 regression(s).

## OAR scope
- DISMM in (ND, PD) = 1011 of 2183 material-plant positions; VB = 45; excluded = 1127. Reconciliation: UNEXPLAINED REMAINDER 26.

## Plant coverage
- Plant 1500 has MARC rows over OData: the I13 s7.2 gap is an extract problem, not a SAP one.

## I13 aging
- Movement history over OData spans 4894 days against the 731 FR-6 needs - sufficient, re-extract.

## Coverage
- MAKT covers 100.1% of MaterialSet over OData (extract showed 8.2%).

## I08 80-series
- 26 80-series materials visible on MaterialSet.

## Dictionary
- EKBE Vgabe distinct values over OData: none. Reconcile against the dictionary (1, 2) and I13 s7.1 (E).

## Slowest calls (W8.1 performance baseline)
- 4.2s  /sap/opu/odata/sap/ZMM_KPI02_TAB_SRV/ChangeDocItemSet/$count  
- 4.0s  /sap/opu/odata/sap/ZMM_KPI02_TAB_SRV/ChangeDocItemSet  $top=5&$format=json
- 3.9s  /sap/opu/odata/sap/ZMM_KPI02_TAB_SRV/ChangeDocItemSet  $top=5&$skip=5&$format=json
- 3.8s  /sap/opu/odata/sap/ZMM_KPI02_TAB_SRV/ChangeDocItemSet  $inlinecount=allpages&$top=1&$format=json
- 3.1s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/GoodsMovementItemSet  $top=1000&$skip=52000&$format=json

## Confirmed defect register

- **B1**: /$count returns HTTP 500 on PurchaseRequisitionSet and GoodsMovementItemSet
- **F1**: 85 of 230 properties silently ignore $filter (impossible-value test returns the set total)
- **F3**: only eq and substringof are honoured; ne and gt/ge/lt/le on strings are not. startswith IS honoured -- the earlier probes failed because they passed an unpadded prefix. MATNR is ALPHA-converted, so '80' matches nothing while '0000000080' returns a count. SAP confirmed the same in SE11/SE16N.
- **F4**: $skip without $orderby produces duplicate and missing rows across pages
- **R1**: ReservationItemSet: ignored $filter property flagged to NTT as the priority fix
- **R2**: ReservationItemSet returns exactly 1,000 rows against 105,848 in the extract (suspected page cap)
