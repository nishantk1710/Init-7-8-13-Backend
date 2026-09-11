"""
VZI CPI OData discovery v4: $metadata, $count, behavioural probes and dictionary gap report.
 
Credentials come from a .env file next to this script (or --env-file / real environment
variables, which always win over the file). Never hard-code them. See .env.example:
 
  CPI_CLIENT_ID=...
  CPI_CLIENT_SECRET=...
  CPI_TOKEN_URL=https://vzicpinonprod.authentication.in30.hana.ondemand.com/oauth/token
  CPI_BASE_URL=https://vzicpinonprod.it-cpi021-rt.cfapps.in30.hana.ondemand.com
 
Usage:
  python cpi_discovery.py --dictionary VZI_Entity_Dictionary_I07_I08_I13_v1_0.xlsx --out ./discovery
  python cpi_discovery.py --only-probes --out ./discovery        # skip metadata and $count sweep
 
Outputs in --out:
  metadata_<service>.xml           raw EDMX as returned
  entity_sets.csv                  service, set, entity type, keys, property count, sap:pageable
  properties.csv                   service, set, property, type, nullable, is_key, maxlength, precision, scale,
                                   sap:filterable, sap:sortable
  counts.csv                       service, set, $count (or error)
  fr9_check.txt                    ChangeDocItemSet counts for FR-9 (object class, table, field level)
  probes.csv                       every behavioural probe: purpose, path, query, status, elapsed, result, rows, __next
  key_collapse.txt                 ChangeDocItem distinct declared key vs full CDPOS composite key
  filter_support.csv               per property: is a $filter on it honoured, ignored, or rejected (impossible-value test)
  marc_changes_summary.txt         all MATERIAL/MARC CDPOS rows pulled and aggregated client-side: Fname x Chngind,
                                   DISMM old->new transitions (adoption-tracking feasibility, W4.7)
  mrp_type_profile.txt             MaterialPlantSet pulled in full: Dismm x Werks, reorder-point population per MRP type
  material_number_profile.txt      MaterialSet pulled in full: Matnr length and prefix profile, 80-series presence
  repair_po_check.txt              items of every ZREP purchase order: Pstyp / Knttp / Matnr per line (I08 convention)
  operator_support.csv             which $filter operators work on properties that honour eq: ne, gt/ge/lt/le, or,
                                   startswith, substringof, date ranges (decides whether delta loads by date are possible)
  paging_stability.txt             same set pulled twice with and without $orderby; duplicate/missing keys per method,
                                   and filtered-pull row counts reconciled against filtered $count (Dismm values)
  calls.csv                        every HTTP call with elapsed seconds (performance baseline for W8.1)
  errors.txt / errors.csv          every non-2xx call with trace headers and body
  dictionary_gaps.csv              dictionary field vs actual property, per SAP object (needs --dictionary)
"""
import argparse, csv, os, sys, time, json
import xml.etree.ElementTree as ET
from pathlib import Path
 
try:
    import requests
except ImportError:
    sys.exit("pip install requests openpyxl")
 
SERVICES = {
    "ZVZI_KPI02_SHARED_SRV": ["GoodsMovementItemSet", "InfoRecordOrgSet", "InfoRecordSet", "MaterialDescriptionSet",
                              "MaterialDocumentHeaderSet", "MaterialPlantSet", "MaterialSet", "POHistorySet",
                              "POScheduleLineSet", "PurchaseOrderItemSet", "PurchaseOrderSet", "PurchaseRequisitionSet",
                              "StorageLocationStockSet", "VendorSet"],
    "ZMM_KPI02_SRV": ["BatchStockSet", "ChangeDocHeaderSet", "ChangeDocItemSet", "MaterialValuationSet",
                      "MonthlyMovementStatisticSet", "ReservationItemSet", "StockMovementStatisticSet"],
}
 
# Map SAP table -> (service, set) so dictionary rows can be compared to real properties
TABLE_TO_SET = {
    "MARA": ("ZVZI_KPI02_SHARED_SRV", "MaterialSet"), "MAKT": ("ZVZI_KPI02_SHARED_SRV", "MaterialDescriptionSet"),
    "MARC": ("ZVZI_KPI02_SHARED_SRV", "MaterialPlantSet"), "MARD": ("ZVZI_KPI02_SHARED_SRV", "StorageLocationStockSet"),
    "MSEG": ("ZVZI_KPI02_SHARED_SRV", "GoodsMovementItemSet"), "MKPF": ("ZVZI_KPI02_SHARED_SRV", "MaterialDocumentHeaderSet"),
    "EBAN": ("ZVZI_KPI02_SHARED_SRV", "PurchaseRequisitionSet"), "EKKO": ("ZVZI_KPI02_SHARED_SRV", "PurchaseOrderSet"),
    "EKPO": ("ZVZI_KPI02_SHARED_SRV", "PurchaseOrderItemSet"), "EKET": ("ZVZI_KPI02_SHARED_SRV", "POScheduleLineSet"),
    "EKBE": ("ZVZI_KPI02_SHARED_SRV", "POHistorySet"), "EINA": ("ZVZI_KPI02_SHARED_SRV", "InfoRecordSet"),
    "EINE": ("ZVZI_KPI02_SHARED_SRV", "InfoRecordOrgSet"), "LFA1": ("ZVZI_KPI02_SHARED_SRV", "VendorSet"),
    "RESB": ("ZMM_KPI02_SRV", "ReservationItemSet"), "MBEW": ("ZMM_KPI02_SRV", "MaterialValuationSet"),
    "CDHDR": ("ZMM_KPI02_SRV", "ChangeDocHeaderSet"), "CDPOS": ("ZMM_KPI02_SRV", "ChangeDocItemSet"),
    "MCHB": ("ZMM_KPI02_SRV", "BatchStockSet"), "S031": ("ZMM_KPI02_SRV", "MonthlyMovementStatisticSet"),
    "S032": ("ZMM_KPI02_SRV", "StockMovementStatisticSet"),
}
 
SHARED = "sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV"
ZMM = "sap/opu/odata/sap/ZMM_KPI02_SRV"
 
# ---------------------------------------------------------------------------------------------
# Behavioural probes. Each row: (purpose, dev-plan task, service path, entity set, odata query).
# A query ending in /$count returns a bare number. Anything else is fetched as JSON so the
# row payload can be parsed (d.results, d.__next, d.__count).
# ---------------------------------------------------------------------------------------------
JSON = "$format=json"
PROBES = [
    # E1: separate "no data in client" from "filtered out by the DPC"
    ("E1 zero-count set: does $top=1 return a row",           "W2.6/W6.2", ZMM, "ReservationItemSet",          f"$top=1&{JSON}"),
    ("E1 zero-count set: does $top=1 return a row",           "W2.6",      ZMM, "MaterialValuationSet",        f"$top=1&{JSON}"),
    ("E1 zero-count set: does $top=1 return a row",           "W2.6/W3.5", ZMM, "MonthlyMovementStatisticSet", f"$top=1&{JSON}"),
    # B1: isolate $count handling from the GET_ENTITYSET SELECT
    ("B1 failing set: does $top=1 work where $count dumps",   "W2.3",      SHARED, "PurchaseRequisitionSet",   f"$top=1&{JSON}"),
    ("B1 failing set: does $top=1 work where $count dumps",   "W2.3",      SHARED, "GoodsMovementItemSet",     f"$top=1&{JSON}"),
    # Paging despite sap:pageable="false" on every entity set
    ("Paging: first page",                                    "W2.3/W3.2", ZMM, "ChangeDocItemSet", f"$top=5&{JSON}"),
    ("Paging: second page, rows must differ from first",      "W2.3/W3.2", ZMM, "ChangeDocItemSet", f"$top=5&$skip=5&{JSON}"),
    ("Paging: inline count and __next presence",              "W2.3/W3.2", ZMM, "ChangeDocItemSet", f"$inlinecount=allpages&$top=1&{JSON}"),
    ("Paging: large set page-until-short-page viability",     "W2.3/W3.1", SHARED, "MaterialDocumentHeaderSet", f"$top=1000&$skip=40000&{JSON}"),
    ("Paging: does $orderby work despite sortable=false",     "W2.3",      SHARED, "PurchaseOrderSet", f"$orderby=Aedat desc&$top=3&{JSON}"),
    # FR-9 field level: adoption tracking on MARC planning fields (W4.7)
    ("FR-9: MARC field changes DISMM",                        "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'DISMM'"),
    ("FR-9: MARC field changes EISBE",                        "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'EISBE'"),
    ("FR-9: MARC field changes MINBE",                        "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'MINBE'"),
    ("FR-9: MARC field changes MABST",                        "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'MABST'"),
    ("FR-9: conversions to Min-Max (DISMM new value VB)",     "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'DISMM' and Value_new eq 'VB'"),
    ("FR-9: header join sample, MATERIAL class",              "W4.7", ZMM, "ChangeDocHeaderSet", f"$filter=Objectclas eq 'MATERIAL'&$top=3&{JSON}"),
    # OAR classifier on MRP type (W2.4). Counts should reconcile to the MaterialPlantSet total.
    ("OAR classifier: MRP type VB (Min-Max)",                 "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'VB'"),
    ("OAR classifier: MRP type ND (OAR)",                     "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'ND'"),
    ("OAR classifier: MRP type PD (OAR)",                     "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'PD'"),
    ("OAR classifier: MRP type V1 (ruling pending)",          "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'V1'"),
    ("OAR classifier: MRP type blank (ruling pending)",       "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq ''"),
    ("OAR classifier: VB rows carrying a reorder point",      "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'VB' and Minbe gt 0"),
    ("OAR classifier: ND/PD rows carrying a reorder point (should be ~0)", "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=(Dismm eq 'ND' or Dismm eq 'PD') and Minbe gt 0"),
    # I08 repair-PO convention and 80-series detection (W5.1, W5.2)
    ("I08: PO items with item category 3",                    "W5.2", SHARED, "PurchaseOrderItemSet/$count", "$filter=Pstyp eq '3'"),
    ("I08: PO headers with document type ZREP",               "W5.2", SHARED, "PurchaseOrderSet/$count",     "$filter=Bsart eq 'ZREP'"),
    ("I08: 80-series material PO lines (startswith)",         "W5.1", SHARED, "PurchaseOrderItemSet/$count", "$filter=startswith(Matnr,'80')"),
    ("I08: 80-series materials in master (startswith)",       "W5.1", SHARED, "MaterialSet/$count",          "$filter=startswith(Matnr,'80')"),
    ("I08: text-only PO lines (no material)",                 "W5.5", SHARED, "PurchaseOrderItemSet/$count", "$filter=Matnr eq ''"),
    # Totals for the two sets whose /$count dumps, via $inlinecount on a one-row page
    ("B1 failing set: total via $inlinecount=allpages",         "W2.3", SHARED, "PurchaseRequisitionSet", f"$inlinecount=allpages&$top=1&{JSON}"),
    ("B1 failing set: total via $inlinecount=allpages",         "W2.3", SHARED, "GoodsMovementItemSet",   f"$inlinecount=allpages&$top=1&{JSON}"),
    # startswith control: a prefix known to exist from the MaterialPlantSet sample; 0 here means startswith is unsupported
    ("I08 control: startswith on a known prefix (2227)",       "W5.1", SHARED, "MaterialSet/$count", "$filter=startswith(Matnr,'2227')"),
    ("I08: 80-series by range on 8-digit numbers",             "W5.1", SHARED, "MaterialSet/$count", "$filter=Matnr ge '80000000' and Matnr le '80999999'"),
    ("I08: 80-series by range on 18-digit padded numbers",     "W5.1", SHARED, "MaterialSet/$count", "$filter=Matnr ge '000000000080000000' and Matnr le '000000000080999999'"),
    # Envelope samples for the W2.1 client library
    ("W2.1 envelope: decimal and date typing sample",         "W2.1", SHARED, "MaterialPlantSet",     f"$top=2&{JSON}"),
    ("W2.1 envelope: Netpr/Netwr arrive as strings",          "W2.1", SHARED, "PurchaseOrderItemSet", f"$top=2&{JSON}"),
    ("W2.1 envelope: $select support",                        "W2.1", SHARED, "MaterialPlantSet",     f"$select=Matnr,Werks,Dismm&$top=2&{JSON}"),
]
 
CPI_PATH = "/http/SAPECC/OdataConsumption"
NS = {"edmx": "http://schemas.microsoft.com/ado/2007/06/edmx", "edm": "http://schemas.microsoft.com/ado/2008/09/edm",
      "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"}
SAP_NS = "http://www.sap.com/Protocols/SAPData"
 
 
DEFAULT_ENV_FILE = Path(__file__).resolve().parent / ".env"
 
 
def load_env_file(path):
    """Minimal .env loader (no dependency). Real environment variables take precedence."""
    path = Path(path)
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key, value)
    return True
 
 
def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing {name}: set it in {DEFAULT_ENV_FILE} (see .env.example) or in the environment")
    return v
 
 
def get_token(session):
    r = session.post(env("CPI_TOKEN_URL"), data={"grant_type": "client_credentials"},
                     auth=(env("CPI_CLIENT_ID"), env("CPI_CLIENT_SECRET")), timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]
 
 
# Every non-2xx response, kept for the SAP-team failure report (errors.txt / errors.csv).
FAILURES = []
# Every call, for the performance baseline (calls.csv).
CALLS = []
# Sets whose /$count has already returned 500 in this run: go straight to $inlinecount for them.
COUNT_DUMPS = set()
# Headers CPI / CF / the SAP gateway use to tie a request to a server-side log entry.
TRACE_HEADERS = ("x-correlationid", "sap-messageprocessinglogid", "x-vcap-request-id", "x-request-id",
                 "sap-message", "dataserviceversion", "content-type", "date")
 
 
def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
 
 
def record_failure(response, api_path, api_query):
    FAILURES.append({
        "utc": utcnow(),
        "api_path": api_path, "api_query": api_query, "status": response.status_code,
        "elapsed_s": round(response.elapsed.total_seconds(), 1) if response.elapsed is not None else None,
        "headers": {k: v for k, v in response.headers.items() if k.lower() in TRACE_HEADERS},
        "body": response.text[:4000],
    })
 
 
def cpi_get(session, token, api_path, api_query="", retries=3):
    url = env("CPI_BASE_URL").rstrip("/") + CPI_PATH
    t0 = time.time()
    for attempt in range(retries):
        r = session.get(url, params={"APIPath": api_path, "APIQuery": api_query},
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, application/xml;q=0.9, */*;q=0.8"},
                        timeout=120)
        if r.status_code == 401 and attempt == 0:
            token = get_token(session)
            continue
        if r.status_code >= 500 and attempt < retries - 1:
            time.sleep(2 ** attempt)
            continue
        break
    CALLS.append([utcnow(), "/" + api_path.lstrip("/"), api_query, r.status_code,
                  round(time.time() - t0, 1), len(r.content)])
    if not r.ok:
        record_failure(r, api_path, api_query)
    return r, token
 
 
def parse_json_feed(text):
    """OData v2 JSON envelope -> (rows, next_link, inline_count). Tolerates single-entity and error shapes."""
    try:
        d = json.loads(text).get("d", {})
    except (ValueError, AttributeError):
        return [], None, None
    if isinstance(d, dict) and "results" in d:
        return d.get("results", []), d.get("__next"), d.get("__count")
    if isinstance(d, list):
        return d, None, None
    return ([d] if d else []), None, None
 
 
def strip_meta(row):
    return {k: v for k, v in row.items() if k != "__metadata"}
 
 
def parse_edmx(xml_text):
    """Return sets: {set_name: (entity_type, pageable)}, types: {type_name: {keys:set, props:[dict]}}"""
    root = ET.fromstring(xml_text)
    types, sets = {}, {}
    for schema in root.iter(f"{{{NS['edm']}}}Schema"):
        for et in schema.findall("edm:EntityType", NS):
            name = et.get("Name")
            keys = {pr.get("Name") for pr in et.findall("edm:Key/edm:PropertyRef", NS)}
            props = []
            for p in et.findall("edm:Property", NS):
                props.append({
                    "name": p.get("Name"), "type": p.get("Type"), "nullable": p.get("Nullable", "true"),
                    "maxlength": p.get("MaxLength", ""), "precision": p.get("Precision", ""), "scale": p.get("Scale", ""),
                    "filterable": p.get(f"{{{SAP_NS}}}filterable", ""), "sortable": p.get(f"{{{SAP_NS}}}sortable", ""),
                    "label": p.get(f"{{{SAP_NS}}}label", ""),
                })
            types[name] = {"keys": keys, "props": props}
        for es in schema.findall("edm:EntityContainer/edm:EntitySet", NS):
            sets[es.get("Name")] = (es.get("EntityType", "").split(".")[-1], es.get(f"{{{SAP_NS}}}pageable", ""))
    return sets, types
 
 
def load_dictionary_fields(path):
    """Sheet '2. Field Specification': rows -> (SAP Object, Field, key flag)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["2. Field Specification"]
    out = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row[0] or not row[1]:
            continue
        out.append((str(row[0]).strip(), str(row[1]).strip(), row[3]))
    return out
 
 
def run_sweep(s, token, out, skip_counts):
    """$metadata and $count for every service. Returns (token, actual) where actual maps (svc,set) -> keys/props."""
    all_sets_rows, prop_rows, count_rows = [], [], []
    actual, actual_props, totals = {}, {}, {}
    for svc, expected_sets in SERVICES.items():
        r, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/$metadata")
        (out / f"metadata_{svc}.xml").write_text(r.text, encoding="utf-8")
        if r.status_code != 200:
            print(f"[{svc}] $metadata HTTP {r.status_code}: {r.text[:200]}")
            continue
        sets, types = parse_edmx(r.text)
        print(f"[{svc}] {len(sets)} entity sets in $metadata; expected {len(expected_sets)} from technical list")
        missing = set(expected_sets) - set(sets); extra = set(sets) - set(expected_sets)
        if missing: print(f"   MISSING vs technical list: {sorted(missing)}")
        if extra:   print(f"   EXTRA in $metadata:          {sorted(extra)}")
        for set_name, (type_name, pageable) in sets.items():
            t = types.get(type_name, {"keys": set(), "props": []})
            all_sets_rows.append([svc, set_name, type_name, ";".join(sorted(t["keys"])), len(t["props"]), pageable])
            actual[(svc, set_name)] = {"keys": t["keys"], "props": [p["name"] for p in t["props"]]}
            actual_props[(svc, set_name)] = t["props"]
            for p in t["props"]:
                prop_rows.append([svc, set_name, p["name"], p["type"], p["nullable"], "K" if p["name"] in t["keys"] else "",
                                  p["maxlength"], p["precision"], p["scale"], p["filterable"], p["sortable"], p["label"]])
            if not skip_counts:
                rc, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/{set_name}/$count")
                count_rows.append([svc, set_name, rc.text.strip() if rc.status_code == 200 else f"HTTP {rc.status_code}",
                                   round(rc.elapsed.total_seconds(), 1)])
                if rc.status_code == 200 and rc.text.strip().isdigit():
                    totals[(svc, set_name)] = int(rc.text.strip())
                else:  # /$count dumps: take the total from $inlinecount instead
                    COUNT_DUMPS.add(f"sap/opu/odata/sap/{svc}/{set_name}")
                    r2, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/{set_name}", f"$inlinecount=allpages&$top=1&{JSON}", retries=1)
                    if r2.status_code == 200:
                        _, _, inline = parse_json_feed(r2.text)
                        if inline is not None and str(inline).isdigit():
                            totals[(svc, set_name)] = int(inline)
                            count_rows[-1][2] = f"HTTP 500 on /$count; $inlinecount={inline}"
                print(f"   {set_name:32s} count = {count_rows[-1][2]}  ({count_rows[-1][3]}s)")
 
    with open(out / "entity_sets.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["service", "entity_set", "entity_type", "keys", "property_count", "sap_pageable"]); w.writerows(all_sets_rows)
    with open(out / "properties.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "entity_set", "property", "type", "nullable", "is_key", "maxlength", "precision", "scale",
                    "sap_filterable", "sap_sortable", "sap_label"])
        w.writerows(prop_rows)
    if not skip_counts:
        with open(out / "counts.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["service", "entity_set", "count", "elapsed_s"]); w.writerows(count_rows)
    return token, actual, actual_props, totals
 
 
def run_fr9(s, token, out):
    fr9 = []
    for flt in ["Objectclas eq 'MATERIAL'", "Objectclas eq 'MATERIAL' and Tabname eq 'MARC'", "Objectclas eq 'BANF'"]:
        rc, token = cpi_get(s, token, f"{ZMM}/ChangeDocItemSet/$count", f"$filter={flt}")
        fr9.append(f"{flt:55s} -> {rc.text.strip() if rc.status_code == 200 else 'HTTP ' + str(rc.status_code) + ' ' + rc.text[:120]}")
    (out / "fr9_check.txt").write_text("\n".join(fr9) + "\n(Property names Objectclas / Tabname confirmed against live $metadata on 2026-09-08. "
                                       "Field-level counts for DISMM/EISBE/MINBE/MABST are in probes.csv.)\n")
    print("\nFR-9 check:\n  " + "\n  ".join(fr9))
    return token
 
 
def run_probes(s, token, out):
    rows = []
    first_page_keys = None
    print("\nProbes:")
    for purpose, task, svc_path, target, query in PROBES:
        api_path = f"{svc_path}/{target}"
        r, token = cpi_get(s, token, api_path, query, retries=2)
        elapsed = round(r.elapsed.total_seconds(), 1)
        result, nrows, has_next, note = "", "", "", ""
        if r.status_code != 200:
            result = f"HTTP {r.status_code}"
        elif target.endswith("/$count"):
            result = r.text.strip()
        else:
            data, nxt, inline = parse_json_feed(r.text)
            nrows = len(data)
            has_next = "yes" if nxt else "no"
            result = f"{nrows} row(s)" + (f", __count={inline}" if inline is not None else "")
            if data:
                note = json.dumps(strip_meta(data[0]), default=str)[:300]
            # paging sanity: compare first and second ChangeDocItemSet pages
            if target == "ChangeDocItemSet" and query.startswith("$top=5&$format"):
                first_page_keys = {tuple(strip_meta(x).values()) for x in data}
            elif target == "ChangeDocItemSet" and "$skip=5" in query and first_page_keys is not None:
                second = {tuple(strip_meta(x).values()) for x in data}
                overlap = len(first_page_keys & second)
                note = f"overlap with first page = {overlap} of {len(second)} ({'SKIP IGNORED' if overlap and nrows else 'skip honoured'})"
        rows.append([utcnow(), purpose, task, "/" + api_path, query, r.status_code, elapsed, result, nrows, has_next, note])
        print(f"   [{r.status_code}] {purpose:62s} -> {result}  ({elapsed}s)")
 
    with open(out / "probes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["utc", "purpose", "dev_plan_task", "sap_path", "odata_query", "http_status", "elapsed_s", "result",
                    "rows_returned", "has_next", "note_or_sample"])
        w.writerows(rows)
 
    # OAR classifier reconciliation: VB + ND + PD + V1 + blank should equal the MaterialPlantSet total
    counts = {r[1]: r[7] for r in rows if r[1].startswith("OAR classifier: MRP type")}
    total_r, token = cpi_get(s, token, f"{SHARED}/MaterialPlantSet/$count")
    try:
        parts = {k.split("MRP type ")[1].split(" ")[0]: int(v) for k, v in counts.items()}
        total = int(total_r.text.strip())
        print(f"\nOAR classifier reconciliation: {parts} sum={sum(parts.values())} vs MaterialPlantSet total={total}"
              f" -> {'reconciles' if sum(parts.values()) == total else 'UNEXPLAINED REMAINDER ' + str(total - sum(parts.values()))}")
    except (ValueError, IndexError):
        print("\nOAR classifier reconciliation: one or more MRP-type counts failed; see probes.csv")
    return token
 
 
def run_key_collapse(s, token, out, sample=500):
    """Quantify Issue C1 on ChangeDocItem: distinct declared key vs full CDPOS composite key over a live sample."""
    declared = ("Objectclas", "Objectid", "Changenr")
    composite = declared + ("Tabname", "Tabkey", "Fname", "Chngind")
    r, token = cpi_get(s, token, f"{ZMM}/ChangeDocItemSet",
                       f"$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC'&$top={sample}&{JSON}", retries=2)
    lines = [f"ChangeDocItemSet key collapse check, {utcnow()}, sample = MATERIAL/MARC $top={sample}"]
    if r.status_code != 200:
        lines.append(f"HTTP {r.status_code}: could not fetch sample")
    else:
        data, _, _ = parse_json_feed(r.text)
        d_keys = {tuple(x.get(k) for k in declared) for x in data}
        c_keys = {tuple(x.get(k) for k in composite) for x in data}
        lines += [f"rows returned                     : {len(data)}",
                  f"distinct on declared key (3 cols)  : {len(d_keys)}",
                  f"distinct on composite key (7 cols) : {len(c_keys)}",
                  f"rows unaddressable under declared  : {len(data) - len(d_keys)} ({(len(data) - len(d_keys)) / len(data):.0%})" if data else "no rows",
                  "",
                  "Fields changed in sample (Fname -> rows):"]
        from collections import Counter
        for fname, n in Counter(x.get("Fname") for x in data).most_common(20):
            lines.append(f"  {fname:12s} {n}")
    (out / "key_collapse.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return token
 
 
def fetch_all(s, token, api_path, flt="", page=500, max_pages=40, orderby=""):
    """Client-driven paging with $top/$skip (the gateway emits no __next). Returns (rows, token, pages)."""
    rows, skip, pages = [], 0, 0
    while pages < max_pages:
        q = (f"$filter={flt}&" if flt else "") + (f"$orderby={orderby}&" if orderby else "") + f"$top={page}&$skip={skip}&{JSON}"
        r, token = cpi_get(s, token, api_path, q, retries=2)
        if r.status_code != 200:
            break
        data, _, _ = parse_json_feed(r.text)
        rows += [strip_meta(x) for x in data]
        pages += 1
        if len(data) < page:
            break
        skip += page
    return rows, token, pages
 
 
def count_of(s, token, api_path, flt):
    """Filtered count: /$count first, $inlinecount fallback for the sets whose /$count dumps. Returns (int|None, status, token)."""
    if api_path not in COUNT_DUMPS:
        r, token = cpi_get(s, token, f"{api_path}/$count", f"$filter={flt}", retries=1)
        if r.status_code == 200 and r.text.strip().isdigit():
            return int(r.text.strip()), 200, token
        if r.status_code == 500 and not flt.startswith("Meins"):
            COUNT_DUMPS.add(api_path)
    else:
        class _R: status_code = 500
        r = _R()
    r2, token = cpi_get(s, token, api_path, f"$filter={flt}&$inlinecount=allpages&$top=1&{JSON}", retries=1)
    if r2.status_code == 200:
        _, _, inline = parse_json_feed(r2.text)
        if inline is not None and str(inline).isdigit():
            return int(inline), 200, token
    return None, (r.status_code if r.status_code != 200 else r2.status_code), token
 
 
IMPOSSIBLE = {"Edm.String": "eq 'ZZ~NOPE'", "Edm.Decimal": "eq 987654321.123M",
              "Edm.DateTime": "eq datetime'1900-01-01T00:00:00'"}
 
 
def run_filter_support(s, token, out, actual_props, totals):
    """For every property, filter on a value that cannot exist. Honoured -> 0. Ignored -> the set total. Anything else -> odd."""
    rows = []
    print("\nFilter support sweep (impossible-value test):")
    for (svc, set_name), props in actual_props.items():
        total = totals.get((svc, set_name))
        if not total:
            continue  # empty or unreachable set: the test cannot distinguish anything
        api_path = f"sap/opu/odata/sap/{svc}/{set_name}"
        for p in props:
            lit = IMPOSSIBLE.get(p["type"])
            if not lit:
                rows.append([svc, set_name, p["name"], p["type"], "", "", "NOT_TESTED"]); continue
            n, status, token = count_of(s, token, api_path, f"{p['name']} {lit}")
            if status != 200:
                verdict = f"REJECTED_HTTP_{status}"
            elif n == 0:
                verdict = "HONOURED"
            elif n == total:
                verdict = "IGNORED"
            else:
                verdict = "PARTIAL_OR_ODD"
            rows.append([svc, set_name, p["name"], p["type"], total, n if n is not None else "", verdict])
        v = [r[6] for r in rows if r[0] == svc and r[1] == set_name]
        print(f"   {set_name:32s} honoured={v.count('HONOURED'):2d} ignored={v.count('IGNORED'):2d} "
              f"rejected={sum(x.startswith('REJECTED') for x in v):2d} not_tested={v.count('NOT_TESTED')}")
    with open(out / "filter_support.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "entity_set", "property", "type", "set_total", "count_with_impossible_filter", "verdict"])
        w.writerows(rows)
    return token
 
 
def run_marc_changes(s, token, out):
    """Server-side Fname filters may be ignored, so pull every MATERIAL/MARC change row and aggregate client-side."""
    from collections import Counter
    rows, token, pages = fetch_all(s, token, f"{ZMM}/ChangeDocItemSet", "Objectclas eq 'MATERIAL' and Tabname eq 'MARC'",
                                   orderby="Changenr")
    lines = [f"MATERIAL/MARC change items, {utcnow()}: {len(rows)} rows in {pages} page(s)", "", "Fname x Chngind:"]
    for (fn, ci), n in Counter((r.get("Fname"), r.get("Chngind")) for r in rows).most_common():
        lines.append(f"  {fn or '<blank>':12s} {ci or '-':2s} {n}")
    planning = [r for r in rows if r.get("Fname") in ("DISMM", "EISBE", "MINBE", "MABST")]
    lines += ["", f"Planning-field changes (DISMM/EISBE/MINBE/MABST): {len(planning)}"]
    if planning:
        lines.append("DISMM transitions old -> new:")
        for (o, n_), c in Counter((r.get("Value_old"), r.get("Value_new")) for r in planning if r.get("Fname") == "DISMM").most_common():
            lines.append(f"  {o or '<blank>':4s} -> {n_ or '<blank>':4s} {c}")
        lines.append("Distinct materials (Objectid) with a planning-field change: "
                     f"{len({r.get('Objectid') for r in planning})}")
    else:
        lines.append("No planning-field change history in this client: adoption tracking (W4.7) can be built but not validated here.")
    (out / "marc_changes_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines[:16]) + ("\n  ..." if len(lines) > 16 else ""))
    return token
 
 
def run_mrp_profile(s, token, out, rows=None):
    from collections import Counter
    if rows is None:
        rows, token, pages = fetch_all(s, token, f"{SHARED}/MaterialPlantSet", orderby="Matnr,Werks")
    else:
        pages = "reused"
    lines = [f"MaterialPlantSet full pull, {utcnow()}: {len(rows)} rows in {pages} page(s)", "", "Dismm x Werks:"]
    for (d, w), n in sorted(Counter((r.get("Dismm") or "<blank>", r.get("Werks")) for r in rows).items()):
        lines.append(f"  {d:8s} {w:6s} {n}")
    lines += ["", "Per MRP type: rows, with reorder point (Minbe>0), with max stock (Mabst>0), with safety stock (Eisbe>0):"]
    def pos(v):
        try: return float(v) > 0
        except (TypeError, ValueError): return False
    for d, grp in sorted(Counter(r.get("Dismm") or "<blank>" for r in rows).items()):
        sub = [r for r in rows if (r.get("Dismm") or "<blank>") == d]
        lines.append(f"  {d:8s} {grp:5d}  rop={sum(pos(r.get('Minbe')) for r in sub):4d}  "
                     f"max={sum(pos(r.get('Mabst')) for r in sub):4d}  ss={sum(pos(r.get('Eisbe')) for r in sub):4d}")
    oar = [r for r in rows if r.get("Dismm") in ("ND", "PD")]
    lines += ["", f"OAR population under the agreed rule (Dismm in ND, PD): {len(oar)} of {len(rows)}; "
                  f"VB (Min-Max): {sum(r.get('Dismm') == 'VB' for r in rows)}; excluded (V1, blank, other): "
                  f"{len(rows) - len(oar) - sum(r.get('Dismm') == 'VB' for r in rows)}"]
    (out / "mrp_type_profile.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return token, rows
 
 
def run_material_profile(s, token, out):
    from collections import Counter
    rows, token, pages = fetch_all(s, token, f"{SHARED}/MaterialSet", orderby="Matnr")
    nums = [r.get("Matnr", "") for r in rows]
    lines = [f"MaterialSet full pull, {utcnow()}: {len(rows)} rows in {pages} page(s)", "", "Matnr length distribution:"]
    for ln, n in sorted(Counter(len(x) for x in nums).items()):
        lines.append(f"  len {ln:2d}: {n}")
    lines.append(""); lines.append("Leading two characters after stripping leading zeros:")
    for pre, n in Counter(x.lstrip("0")[:2] for x in nums).most_common(15):
        lines.append(f"  {pre or '<none>':4s} {n}")
    eighty = [x for x in nums if x.lstrip("0").startswith("80")]
    lines += ["", f"80-series materials present (after stripping leading zeros): {len(eighty)}"
                  + ("  e.g. " + ", ".join(eighty[:5]) if eighty else "  -> I08 80-series detection cannot be validated in this client")]
    lines += ["", "Material type (Mtart) distribution:"]
    for mt, n in Counter(r.get("Mtart") for r in rows).most_common():
        lines.append(f"  {mt or '<blank>':6s} {n}")
    (out / "material_number_profile.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return token
 
 
def run_repair_po_check(s, token, out):
    """Pstyp filters may be ignored server-side, so read every line of every ZREP PO and tabulate client-side."""
    from collections import Counter
    heads, token, _ = fetch_all(s, token, f"{SHARED}/PurchaseOrderSet", "Bsart eq 'ZREP'")
    lines = [f"ZREP purchase orders, {utcnow()}: {len(heads)} header(s)"]
    all_items = []
    for h in heads:
        items, token, _ = fetch_all(s, token, f"{SHARED}/PurchaseOrderItemSet", f"Ebeln eq '{h.get('Ebeln')}'")
        all_items += items
        lines.append(f"  {h.get('Ebeln')}  vendor {h.get('Lifnr')}  {str(h.get('Aedat'))[:22]}  items={len(items)}  "
                     f"pstyp={dict(Counter(i.get('Pstyp') for i in items))}  knttp={dict(Counter(i.get('Knttp') for i in items))}")
    if all_items:
        lines += ["", "Across all ZREP lines: Pstyp x Knttp:"]
        for (p, k), n in Counter((i.get("Pstyp"), i.get("Knttp")) for i in all_items).most_common():
            lines.append(f"  pstyp={p or '<blank>'} knttp={k or '<blank>'} {n}")
        lines.append(f"Lines with a material number: {sum(bool(i.get('Matnr')) for i in all_items)} of {len(all_items)}; "
                     f"80-series: {sum(i.get('Matnr','').lstrip('0').startswith('80') for i in all_items)}")
        lines.append("Note: if every Ebeln filter returned the full set (11,074 items), the Ebeln filter is ignored too; see filter_support.csv.")
    (out / "repair_po_check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return token
 
 
# (set path, property, operator expression, what a working operator should return relative to the eq baseline)
# 'expected' is checked client-side where a full pull exists; otherwise the verdict is heuristic:
#   0 rows where data must exist -> UNSUPPORTED_EMPTY, set total -> IGNORED, anything else -> WORKS_OR_PLAUSIBLE
OPERATOR_PROBES = [
    ("MaterialPlantSet", "Matnr ne '22271519'",                        "ne"),
    ("MaterialPlantSet", "Matnr ge '8000000000' and Matnr le '8099999999'", "ge/le range on string"),
    ("MaterialPlantSet", "Matnr gt '5'",                               "gt on string"),
    ("MaterialPlantSet", "Dismm eq 'ND' or Dismm eq 'PD'",             "or on honoured property"),
    ("MaterialPlantSet", "startswith(Matnr,'8')",                      "startswith"),
    ("MaterialPlantSet", "substringof('800',Matnr)",                   "substringof"),
    ("MaterialPlantSet", "Werks eq '1300' and Dismm eq 'PD'",          "and across two honoured properties"),
    ("MaterialDocumentHeaderSet", "Budat ge datetime'2026-01-01T00:00:00'",   "date ge (delta load by posting date)"),
    ("MaterialDocumentHeaderSet", "Budat ge datetime'2013-01-01T00:00:00' and Budat lt datetime'2014-01-01T00:00:00'", "date range"),
    ("MaterialDocumentHeaderSet", "Budat eq datetime'2013-09-27T00:00:00'",   "date eq (control, sample row date)"),
    ("PurchaseOrderSet", "Aedat ge datetime'2026-01-01T00:00:00'",     "date ge on PO header"),
    ("ChangeDocHeaderSet", "Udate ge datetime'2026-01-01T00:00:00'",   "date ge on CDHDR (delta for FR-9)"),
    ("ChangeDocHeaderSet", "Objectclas eq 'MATERIAL' and Udate ge datetime'2018-01-01T00:00:00'", "eq + date ge"),
    ("ChangeDocItemSet", "Objectclas eq 'MATERIAL' and (Tabname eq 'MARC' or Tabname eq 'MARA')", "or inside parentheses"),
    ("PurchaseOrderItemSet", "Ebeln eq '4500000001' or Ebeln eq '4500000002'", "or on key"),
    ("StockMovementStatisticSet", "Letztbew ge datetime'2020-01-01T00:00:00'", "date ge on S032"),
]
SET_TO_SVC = {name: svc for svc, names in SERVICES.items() for name in names}
 
 
def run_operator_support(s, token, out, totals, mp_rows):
    rows = []
    print("\nOperator support:")
    for set_name, expr, label in OPERATOR_PROBES:
        svc = SET_TO_SVC[set_name]
        api_path = f"sap/opu/odata/sap/{svc}/{set_name}"
        n, status, token = count_of(s, token, api_path, expr)
        total = totals.get((svc, set_name))
        expected = ""
        if set_name == "MaterialPlantSet" and mp_rows:
            m = [r.get("Matnr", "") for r in mp_rows]
            exp_map = {
                "ne": sum(x != "22271519" for x in m),
                "ge/le range on string": sum("8000000000" <= x <= "8099999999" for x in m),
                "gt on string": sum(x > "5" for x in m),
                "or on honoured property": sum(r.get("Dismm") in ("ND", "PD") for r in mp_rows),
                "startswith": sum(x.startswith("8") for x in m),
                "substringof": sum("800" in x for x in m),
                "and across two honoured properties": sum(r.get("Werks") == "1300" and r.get("Dismm") == "PD" for r in mp_rows),
            }
            expected = exp_map.get(label, "")
        if status != 200:
            verdict = f"REJECTED_HTTP_{status}"
        elif expected != "":
            verdict = "WORKS" if n == expected else ("IGNORED" if n == total else ("UNSUPPORTED_EMPTY" if n == 0 else "WRONG_RESULT"))
        elif n == 0:
            verdict = "UNSUPPORTED_EMPTY_OR_NO_DATA"
        elif n == total:
            verdict = "IGNORED"
        else:
            verdict = "PLAUSIBLE"
        rows.append([svc, set_name, label, expr, total, n if n is not None else "", expected, verdict])
        print(f"   {set_name:28s} {label:44s} -> {n} (expected {expected if expected != '' else '?'}, total {total}) {verdict}")
    with open(out / "operator_support.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "entity_set", "operator", "filter", "set_total", "count", "expected_client_side", "verdict"])
        w.writerows(rows)
    return token
 
 
def run_paging_stability(s, token, out, totals):
    """Pull MaterialPlantSet twice without $orderby and once with; a stable pull has zero duplicate and zero missing keys."""
    api_path = f"{SHARED}/MaterialPlantSet"
    total = totals.get(("ZVZI_KPI02_SHARED_SRV", "MaterialPlantSet"))
    key = lambda r: (r.get("Matnr"), r.get("Werks"))
    a, token, _ = fetch_all(s, token, api_path)
    b, token, _ = fetch_all(s, token, api_path)
    c, token, _ = fetch_all(s, token, api_path, orderby="Matnr,Werks")
    lines = [f"MaterialPlantSet paging stability, {utcnow()}, $count total = {total}"]
    for name, rows in (("pull 1, no $orderby", a), ("pull 2, no $orderby", b), ("pull 3, $orderby=Matnr,Werks", c)):
        ks = [key(r) for r in rows]
        lines.append(f"  {name:30s} rows={len(rows):5d} distinct keys={len(set(ks)):5d} duplicates={len(ks) - len(set(ks)):4d} "
                     f"missing vs total={(total or 0) - len(set(ks)):4d}")
    lines.append(f"  keys in pull 1 not in pull 2: {len(set(map(key, a)) - set(map(key, b)))}; pull 2 not in pull 1: {len(set(map(key, b)) - set(map(key, a)))}")
    lines += ["", "Filtered pull vs filtered $count, Dismm values (a mismatch means the filter or the paging is not exact):"]
    for v in ("VB", "ND", "PD", "V1"):
        n, _, token = count_of(s, token, api_path, f"Dismm eq '{v}'")
        pulled, token, _ = fetch_all(s, token, api_path, f"Dismm eq '{v}'", orderby="Matnr,Werks")
        in_full = sum(r.get("Dismm") == v for r in c)
        lines.append(f"  {v:3s} $count={n}  filtered pull rows={len(pulled)}  rows with that value in the ordered full pull={in_full}  "
                     f"{'consistent' if n == len(pulled) == in_full else 'MISMATCH'}")
        if pulled and any(r.get("Dismm") != v for r in pulled):
            lines.append(f"      filtered pull contains rows whose Dismm is not {v}: {dict(__import__('collections').Counter(r.get('Dismm') for r in pulled))}")
    (out / "paging_stability.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return token, c
 
 
def write_dictionary_gaps(actual, dictionary, out):
    rows = []
    for table, field, key in load_dictionary_fields(dictionary):
        svc_set = TABLE_TO_SET.get(table)
        if not svc_set:
            rows.append([table, field, "", "", "NO_SET_MAPPING"]); continue
        act = actual.get(svc_set)
        if not act:
            rows.append([table, field, svc_set[1], "", "SET_NOT_IN_METADATA"]); continue
        props_upper = {p.upper(): p for p in act["props"]}
        hit = props_upper.get(field.upper())
        status = "MATCH" if hit else "MISSING_IN_SAP"
        if hit and key == "K" and hit not in act["keys"]:
            status = "MATCH_BUT_NOT_KEY_IN_SAP"
        rows.append([table, field, svc_set[1], hit or "", status])
    with open(out / "dictionary_gaps.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["sap_table", "dictionary_field", "entity_set", "actual_property", "status"]); w.writerows(rows)
    from collections import Counter
    print("\nDictionary gap summary:", dict(Counter(r[4] for r in rows)))
 
 
def write_logs(out):
    with open(out / "calls.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["utc", "sap_path", "odata_query", "http_status", "elapsed_s", "bytes"]); w.writerows(CALLS)
    if FAILURES:
        entry = env("CPI_BASE_URL").rstrip("/") + CPI_PATH
        lines = [f"{len(FAILURES)} failed call(s). CPI entry point: {entry}",
                 "SAP path = what CPI forwards to the ECC gateway; paste it into /IWFND/GW_CLIENT to reproduce.", ""]
        for i, f in enumerate(FAILURES, 1):
            q = f"?{f['api_query']}" if f["api_query"] else ""
            lines += [f"[{i}] HTTP {f['status']} after {f['elapsed_s']}s at {f['utc']}",
                      f"    SAP path : /{f['api_path'].lstrip('/')}{q}",
                      f"    headers  : {json.dumps(f['headers'])}",
                      f"    body     : {f['body'].strip()[:1000] or '<empty - CPI did not forward the backend error>'}", ""]
        (out / "errors.txt").write_text(chr(10).join(lines), encoding="utf-8")
        with open(out / "errors.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["utc", "sap_path", "api_query", "http_status", "elapsed_s", "body"])
            w.writerows([[f["utc"], "/" + f["api_path"].lstrip("/"), f["api_query"], f["status"], f["elapsed_s"],
                          f["body"].strip()[:500]] for f in FAILURES])
        print(f"\n{len(FAILURES)} failed call(s) -> {out / 'errors.txt'} (also errors.csv)")
 
 
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dictionary", help="Entity dictionary xlsx (optional, for gap report)")
    ap.add_argument("--out", default="./discovery")
    ap.add_argument("--skip-counts", action="store_true", help="run $metadata but not the per-set $count")
    ap.add_argument("--skip-probes", action="store_true", help="run the original sweep only")
    ap.add_argument("--only-probes", action="store_true", help="skip $metadata and $count; run FR-9, probes, key check and profiles only")
    ap.add_argument("--skip-filter-sweep", action="store_true", help="skip the per-property filter support test (~230 calls)")
    ap.add_argument("--skip-profiles", action="store_true", help="skip the full pulls (MARC changes, MRP type, material numbers, ZREP)")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="path to .env (default: alongside this script)")
    args = ap.parse_args()
 
    if load_env_file(args.env_file):
        print(f"loaded env from {Path(args.env_file).resolve()}")
    elif args.env_file != str(DEFAULT_ENV_FILE):
        sys.exit(f"--env-file not found: {args.env_file}")
 
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
 
    s = requests.Session()
    token = get_token(s)
    print("token OK")
 
    actual, actual_props, totals = {}, {}, {}
    if not args.only_probes:
        token, actual, actual_props, totals = run_sweep(s, token, out, args.skip_counts)
 
    token = run_fr9(s, token, out)
 
    if not args.skip_probes:
        token = run_probes(s, token, out)
        token = run_key_collapse(s, token, out)
 
    mp_rows = None
    if not args.skip_profiles:
        token = run_marc_changes(s, token, out)
        if totals:
            token, mp_rows = run_paging_stability(s, token, out, totals)
        token, mp_rows = run_mrp_profile(s, token, out, mp_rows)
        token = run_material_profile(s, token, out)
        token = run_repair_po_check(s, token, out)
        if totals:
            token = run_operator_support(s, token, out, totals, mp_rows)
        else:
            print("\nOperator support and paging stability need the $count totals: run without --only-probes / --skip-counts")
 
    if not args.skip_filter_sweep:
        if not actual_props:
            print("\nFilter sweep needs $metadata and $count: run without --only-probes / --skip-counts")
        else:
            token = run_filter_support(s, token, out, actual_props, totals)
 
    if args.dictionary and actual:
        write_dictionary_gaps(actual, args.dictionary, out)
    elif args.dictionary:
        print("\n--dictionary given with --only-probes: gap report needs $metadata, skipped")
 
    write_logs(out)
    print(f"\nDone. Outputs in {out.resolve()}")
 
 
if __name__ == "__main__":
    main()