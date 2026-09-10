"""
VZI CPI OData discovery: $metadata, $count and dictionary gap report.

Credentials come from a .env file next to this script (or --env-file / real environment
variables, which always win over the file). Never hard-code them. See .env.example:

  CPI_CLIENT_ID=...
  CPI_CLIENT_SECRET=...
  CPI_TOKEN_URL=https://vzicpinonprod.authentication.in30.hana.ondemand.com/oauth/token
  CPI_BASE_URL=https://vzicpinonprod.it-cpi021-rt.cfapps.in30.hana.ondemand.com

Usage:
  python cpi_discovery.py --dictionary VZI_Entity_Dictionary_I07_I08_I13_v1_0.xlsx --out ./discovery

Outputs in --out:
  metadata_<service>.xml           raw EDMX as returned
  entity_sets.csv                  service, set, entity type, keys, property count
  properties.csv                   service, set, property, type, nullable, is_key
  counts.csv                       service, set, $count (or error)
  fr9_check.txt                    ChangeDocItemSet count for OBJECTCLAS MATERIAL / TABNAME MARC
  value_domains.csv                per-value $count for low-cardinality business-rule fields (see VALUE_DOMAIN_PROBES)
  dictionary_gaps.csv              dictionary field vs actual property, per SAP object
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

# Low-cardinality fields that drive business rules: full-scan tally (not a guessed
# candidate list — a filtered $count per guessed value silently misses unexpected
# values, including blanks; this walked into exactly that on Dismm, see §1.6(c))
# written to value_domains.csv. Add entries here as new rules need a standing check.
VALUE_DOMAIN_PROBES = [
    # (service, entity_set, field)
    ("ZVZI_KPI02_SHARED_SRV", "MaterialPlantSet", "Dismm"),
    ("ZVZI_KPI02_SHARED_SRV", "MaterialSet", "Mstae"),
]

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

CPI_PATH = "/http/SAPECC/OdataConsumption"
NS = {"edmx": "http://schemas.microsoft.com/ado/2007/06/edmx", "edm": "http://schemas.microsoft.com/ado/2008/09/edm",
      "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"}


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


def cpi_get(session, token, api_path, api_query="", retries=3):
    url = env("CPI_BASE_URL").rstrip("/") + CPI_PATH
    for attempt in range(retries):
        r = session.get(url, params={"APIPath": api_path, "APIQuery": api_query},
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/xml, application/json;q=0.9, */*;q=0.8"},
                        timeout=120)
        if r.status_code == 401 and attempt == 0:
            token = get_token(session)
            continue
        if r.status_code >= 500 and attempt < retries - 1:
            time.sleep(2 ** attempt)
            continue
        return r, token
    return r, token


def parse_edmx(xml_text):
    """Return sets: {set_name: entity_type}, types: {type_name: {keys:set, props:[(name,type,nullable)]}}"""
    root = ET.fromstring(xml_text)
    types, sets = {}, {}
    for schema in root.iter(f"{{{NS['edm']}}}Schema"):
        for et in schema.findall("edm:EntityType", NS):
            name = et.get("Name")
            keys = {pr.get("Name") for pr in et.findall("edm:Key/edm:PropertyRef", NS)}
            props = [(p.get("Name"), p.get("Type"), p.get("Nullable", "true")) for p in et.findall("edm:Property", NS)]
            types[name] = {"keys": keys, "props": props}
        for es in schema.findall("edm:EntityContainer/edm:EntitySet", NS):
            sets[es.get("Name")] = es.get("EntityType", "").split(".")[-1]
    return sets, types


def load_dictionary_fields(path):
    """Sheet '2. Field Specification': rows -> (SAP Object, Field)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["2. Field Specification"]
    out = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row[0] or not row[1]:
            continue
        out.append((str(row[0]).strip(), str(row[1]).strip(), row[3]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dictionary", help="Entity dictionary xlsx (optional, for gap report)")
    ap.add_argument("--out", default="./discovery")
    ap.add_argument("--skip-counts", action="store_true")
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

    all_sets_rows, prop_rows, count_rows = [], [], []
    actual = {}  # (service,set) -> {"keys":..., "props":[names]}

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
        for set_name, type_name in sets.items():
            t = types.get(type_name, {"keys": set(), "props": []})
            all_sets_rows.append([svc, set_name, type_name, ";".join(sorted(t["keys"])), len(t["props"])])
            actual[(svc, set_name)] = {"keys": t["keys"], "props": [p[0] for p in t["props"]]}
            for name, typ, nullable in t["props"]:
                prop_rows.append([svc, set_name, name, typ, nullable, "K" if name in t["keys"] else ""])
            if not args.skip_counts:
                rc, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/{set_name}/$count")
                count_rows.append([svc, set_name, rc.text.strip() if rc.status_code == 200 else f"HTTP {rc.status_code}"])
                print(f"   {set_name:32s} count = {count_rows[-1][2]}")

    with open(out / "entity_sets.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["service", "entity_set", "entity_type", "keys", "property_count"]); w.writerows(all_sets_rows)
    with open(out / "properties.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["service", "entity_set", "property", "type", "nullable", "is_key"]); w.writerows(prop_rows)
    with open(out / "counts.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["service", "entity_set", "count"]); w.writerows(count_rows)

    # FR-9 feasibility: are MATERIAL / MARC change items exposed?
    fr9 = []
    for flt in ["Objectclas eq 'MATERIAL'", "Objectclas eq 'MATERIAL' and Tabname eq 'MARC'", "Objectclas eq 'BANF'"]:
        rc, token = cpi_get(s, token, "sap/opu/odata/sap/ZMM_KPI02_SRV/ChangeDocItemSet/$count", f"$filter={flt}")
        fr9.append(f"{flt:55s} -> {rc.text.strip() if rc.status_code == 200 else 'HTTP ' + str(rc.status_code) + ' ' + rc.text[:120]}")
    (out / "fr9_check.txt").write_text("\n".join(fr9) + "\n(Property names in the filter assume Objectclas/Tabname; adjust to the real names from properties.csv if the call fails.)\n")
    print("\nFR-9 check:\n  " + "\n  ".join(fr9))

    # Value-distribution probes: full-scan tally, not a guessed candidate list. A
    # filtered $count per guessed value silently misses anything unexpected,
    # including blanks — exactly what happened on Dismm the first time (§1.6(c)).
    domain_rows = []
    if not args.skip_counts:
        from collections import Counter
        for svc, set_name, field in VALUE_DOMAIN_PROBES:
            keys = sorted(actual.get((svc, set_name), {}).get("keys", []))
            order_by = f"&$orderby={','.join(keys)}" if keys else ""
            select = ",".join(dict.fromkeys([field, *keys]))  # dedup, field first
            tally, skip, top = Counter(), 0, 1000
            while True:
                rc, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/{set_name}",
                                     f"$select={select}&$top={top}&$skip={skip}{order_by}&$format=json")
                if rc.status_code != 200:
                    print(f"   [{set_name}.{field}] page skip={skip} HTTP {rc.status_code}")
                    break
                rows = json.loads(rc.text)["d"]["results"]
                for row in rows:
                    tally[row.get(field) or "(blank)"] += 1
                if len(rows) < top:
                    break
                skip += top
            total = sum(tally.values())
            print(f"\n[{set_name}.{field}] value distribution ({total} rows scanned):")
            for v, c in tally.most_common():
                pct = f"{c / total * 100:.1f}%" if total else ""
                domain_rows.append([svc, set_name, field, v, c, pct])
                print(f"   {v:12s} -> {c:>6d} {pct}")
    with open(out / "value_domains.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["service", "entity_set", "field", "value", "count", "pct_of_scanned_total"]); w.writerows(domain_rows)

    # Dictionary gap report
    if args.dictionary:
        rows = []
        for table, field, key in load_dictionary_fields(args.dictionary):
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
    print(f"\nDone. Outputs in {out.resolve()}")


if __name__ == "__main__":
    main()