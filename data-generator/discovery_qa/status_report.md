# VZI CPI OData status, 2026-10-16T06:29:17Z

Run mode: --skip-probes --skip-profiles --skip-filter-sweep --label QA --out data-generator\discovery_qa --env-file .env

Calls: 30  |  failures: 0  |  total elapsed: 168s

## Services
- ZMM_KPI02_ADD_SRV: reachable, 14 entity sets in $metadata.
- ZMM_KPI02_TAB_SRV: reachable, 7 entity sets in $metadata.
- ZMM_KPI02_GP_SRV: reachable, 3 entity sets in $metadata.

## Defects
- B1: /$count succeeded on every reachable set - the defect appears FIXED.

## Field exposure
- 2 FRS-required field(s) absent from the projection; 7 newly exposed since the FRS; 0 regression(s).

## Slowest calls (W8.1 performance baseline)
- 60.7s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/POHistorySet/$count  
- 28.5s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/MaterialDocumentHeaderSet/$count  
- 17.8s  /sap/opu/odata/sap/ZMM_KPI02_TAB_SRV/ReservationItemSet/$count  
- 16.3s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/PurchaseOrderItemSet/$count  
- 13.0s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/PurchaseRequisitionSet/$count  

## Confirmed defect register

- **B1**: /$count returns HTTP 500 on PurchaseRequisitionSet and GoodsMovementItemSet
- **F1**: 85 of 230 properties silently ignore $filter (impossible-value test returns the set total)
- **F3**: only eq and substringof are honoured; ne and gt/ge/lt/le on strings are not. startswith IS honoured -- the earlier probes failed because they passed an unpadded prefix. MATNR is ALPHA-converted, so '80' matches nothing while '0000000080' returns a count. SAP confirmed the same in SE11/SE16N.
- **F4**: $skip without $orderby produces duplicate and missing rows across pages
- **R1**: ReservationItemSet: ignored $filter property flagged to NTT as the priority fix
- **R2**: ReservationItemSet returns exactly 1,000 rows against 105,848 in the extract (suspected page cap)
