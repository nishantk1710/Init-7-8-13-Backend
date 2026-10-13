# VZI CPI OData status, 2026-10-13T07:17:36Z

Run mode: --out data-generator/discovery_qa --label QA --env-file .env --skip-probes --skip-profiles

Calls: 329  |  failures: 146  |  total elapsed: 324s

## Services
- ZMM_KPI02_ADD_SRV: reachable, 14 entity sets in $metadata.
- ZMM_KPI02_TAB_SRV: reachable, 7 entity sets in $metadata.

## Defects
- B1: /$count succeeded on every reachable set - the defect appears FIXED.
- F1: 1 of 157 tested properties silently ignore $filter. Previously 85 of 230.
- R1 (ReservationItemSet priority fix): 0 ignored propert(ies) - appears FIXED.

## Field exposure
- 2 FRS-required field(s) absent from the projection; 7 newly exposed since the FRS; 0 regression(s).

## Slowest calls (W8.1 performance baseline)
- 59.6s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/POHistorySet/$count  
- 28.0s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/MaterialDocumentHeaderSet/$count  
- 18.1s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/POScheduleLineSet/$count  $filter=Ebeln eq 'ZZ~NOPE'
- 16.6s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/POScheduleLineSet/$count  $filter=Ebelp eq 'ZZ~NOPE'
- 16.4s  /sap/opu/odata/sap/ZMM_KPI02_TAB_SRV/ReservationItemSet/$count  

## Confirmed defect register

- **B1**: /$count returns HTTP 500 on PurchaseRequisitionSet and GoodsMovementItemSet
- **F1**: 85 of 230 properties silently ignore $filter (impossible-value test returns the set total)
- **F3**: only eq and substringof are honoured; ne and gt/ge/lt/le on strings are not. startswith IS honoured -- the earlier probes failed because they passed an unpadded prefix. MATNR is ALPHA-converted, so '80' matches nothing while '0000000080' returns a count. SAP confirmed the same in SE11/SE16N.
- **F4**: $skip without $orderby produces duplicate and missing rows across pages
- **R1**: ReservationItemSet: ignored $filter property flagged to NTT as the priority fix
- **R2**: ReservationItemSet returns exactly 1,000 rows against 105,848 in the extract (suspected page cap)
