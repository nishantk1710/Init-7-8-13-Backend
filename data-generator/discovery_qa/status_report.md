# VZI CPI OData status, 2026-09-28T08:41:07Z

Run mode: --fields --out ./discovery_qa

Calls: 2  |  failures: 2  |  total elapsed: 10s

## Services
- ZMM_KPI02_ADD_SRV: $metadata returned HTTP 500 - service unreachable, every dependent check below is unevidenced.
- ZMM_KPI02_TAB_SRV: $metadata returned HTTP 500 - service unreachable, every dependent check below is unevidenced.

## Field exposure
- 0 FRS-required field(s) absent from the projection; 0 newly exposed since the FRS; 0 regression(s).

## Slowest calls (W8.1 performance baseline)
- 5.3s  /sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/$metadata  
- 4.6s  /sap/opu/odata/sap/ZMM_KPI02_TAB_SRV/$metadata  

## Confirmed defect register

- **B1**: /$count returns HTTP 500 on PurchaseRequisitionSet and GoodsMovementItemSet
- **F1**: 85 of 230 properties silently ignore $filter (impossible-value test returns the set total)
- **F3**: only eq and substringof are honoured; ne and gt/ge/lt/le on strings are not. startswith IS honoured -- the earlier probes failed because they passed an unpadded prefix. MATNR is ALPHA-converted, so '80' matches nothing while '0000000080' returns a count. SAP confirmed the same in SE11/SE16N.
- **F4**: $skip without $orderby produces duplicate and missing rows across pages
- **R1**: ReservationItemSet: ignored $filter property flagged to NTT as the priority fix
- **R2**: ReservationItemSet returns exactly 1,000 rows against 105,848 in the extract (suspected page cap)
