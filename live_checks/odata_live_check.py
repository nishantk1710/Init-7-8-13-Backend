#!/usr/bin/env python3
"""Live OData check: every field the initiatives need, against live SAP.

STANDALONE. Imports nothing from ``app/`` -- it is a second opinion on the
pipeline, so it must not share the pipeline's code (a bug in a shared helper
would pass both). It only needs ``requests``, which the deployed app already
has.

What it checks, per table (the 21 on the data-requirements sheet):

  1. $metadata  -- the entity set exists, and which of the fields the
                   initiatives need are projected, with their Edm types.
  2. $count     -- answers, and what it says.
  3. The data   -- every row read in key order (the only safe way to page;
                   unordered paging silently drops and duplicates rows),
                   duplicate keys (= rows lost by paging), fill rate of every
                   required field, date ranges, plant spread.
  4. Delta      -- the same four-probe test the pipeline's delta decisions
                   rest on: total / impossible value (1900-01-01) / one real
                   day / the window since --since. HONOURED only when the
                   impossible value returns 0 AND the real day returns a
                   proper subset. A filter SAP ignores returns HTTP 200 with
                   the whole set -- that is defect F1, and this is how it is
                   caught.
  5. Derived    -- children fetched by their parent's keys (EKPO/EKET/EKBE
                   by EBELN, MSEG by MBLNR, CDPOS by CHANGENR), and whether
                   every row returned really belongs to one of those keys.

Read-only: GET requests through CPI and nothing else. No SAP write-back, no
storage, no database. The JSON report holds counts and fill rates, not rows.

Usage -- on the Azure SSH box, from backend/:

    python live_checks/odata_live_check.py                   # all 21 tables
    python live_checks/odata_live_check.py --tables EKKO EKPO MSEG
    python live_checks/odata_live_check.py --quick           # 1,000-row samples
    python live_checks/odata_live_check.py --since 2025-01-01
    python live_checks/odata_live_check.py --no-delta

Configuration comes from the environment (App Service settings), with a
.env file as a fallback: CPI_BASE_URL, CPI_PATH, CPI_TOKEN_URL, CPI_CLIENT_ID,
CPI_CLIENT_SECRET, optionally CPI_CA_BUNDLE and CPI_TIMEOUT_SECONDS.

Exit code 0 when every table read cleanly, 1 when anything failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

CONFIG_KEYS = (
    "CPI_BASE_URL",
    "CPI_PATH",
    "CPI_TOKEN_URL",
    "CPI_CLIENT_ID",
    "CPI_CLIENT_SECRET",
    "CPI_CA_BUNDLE",
    "CPI_TIMEOUT_SECONDS",
    "STORAGE_URL",
    "AZURE_STORAGE_ACCOUNT_KEY",
    "DATABASE_URL",
)
DEFAULT_CPI_PATH = "/http/SAPECC/OdataConsumption"
DEFAULT_SERVICES = ("ZMM_KPI02_ADD_SRV", "ZMM_KPI02_TAB_SRV", "ZMM_KPI02_GP_SRV")


def _read_dotenv(path: Path) -> dict[str, str]:
    """KEY=VALUE lines. Values are taken verbatim after the first '=' (the
    CPI client id contains a '|'), with surrounding quotes and CR removed."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("\r")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_config(env_file: str | None = None) -> dict[str, str]:
    """Environment first (App Service settings, also as APPSETTING_*), then .env."""
    here = Path(__file__).resolve().parent
    candidates = [Path(env_file)] if env_file else [Path.cwd() / ".env", here.parent / ".env"]
    from_file: dict[str, str] = {}
    for candidate in candidates:
        if candidate.is_file():
            for key, value in _read_dotenv(candidate).items():
                from_file.setdefault(key, value)
    config: dict[str, str] = {}
    for key in CONFIG_KEYS:
        value = (
            os.environ.get(key)
            or os.environ.get(f"APPSETTING_{key}")
            or from_file.get(key)
            or ""
        ).strip()
        if value.startswith("@Microsoft.KeyVault("):
            raise SystemExit(
                f"{key} is an unresolved Key Vault reference in this shell. Export the "
                f"real value (e.g. `export {key}=...`) or pass --env-file."
            )
        config[key] = value
    config["CPI_PATH"] = config["CPI_PATH"] or DEFAULT_CPI_PATH
    return config


# --------------------------------------------------------------------------
# CPI transport (Interface 1: GET <base><path>?APIPath=&APIQuery=)
# --------------------------------------------------------------------------

ACCEPT = "application/json, application/xml;q=0.9, */*;q=0.8"


@dataclass
class Reply:
    status: int
    text: str
    seconds: float

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Cpi:
    """Token + one GET door. Retries 5xx and transport errors, refreshes on 401."""

    def __init__(self, config: dict[str, str], retries: int = 2) -> None:
        missing = [k for k in ("CPI_BASE_URL", "CPI_TOKEN_URL", "CPI_CLIENT_ID", "CPI_CLIENT_SECRET") if not config.get(k)]
        if missing:
            raise SystemExit(f"Missing configuration: {', '.join(missing)} (environment or .env)")
        self.config = config
        self.endpoint = config["CPI_BASE_URL"].rstrip("/") + config["CPI_PATH"]
        self.verify: str | bool = config.get("CPI_CA_BUNDLE") or True
        self.timeout = int(config.get("CPI_TIMEOUT_SECONDS") or 120)
        self.retries = retries
        self.session = requests.Session()
        self.calls = 0
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            if self._token and not force and time.time() < self._expires:
                return self._token
            return self._fetch_token()

    def _fetch_token(self) -> str:
        response = self.session.post(
            self.config["CPI_TOKEN_URL"],
            data={"grant_type": "client_credentials"},
            auth=(self.config["CPI_CLIENT_ID"], self.config["CPI_CLIENT_SECRET"]),
            timeout=30,
            verify=self.verify,
        )
        if not response.ok:
            raise SystemExit(f"Token request failed: HTTP {response.status_code} {response.text[:200]}")
        body = response.json()
        self._token = body["access_token"]
        self._expires = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._token

    def get(self, api_path: str, api_query: str = "") -> Reply:
        last = Reply(0, "no attempt made", 0.0)
        for attempt in range(self.retries + 1):
            started = time.time()
            try:
                response = self.session.get(
                    self.endpoint,
                    params={"APIPath": api_path, "APIQuery": api_query},
                    headers={"Authorization": f"Bearer {self.token()}", "Accept": ACCEPT},
                    timeout=self.timeout,
                    verify=self.verify,
                )
            except requests.RequestException as exc:
                last = Reply(0, f"{type(exc).__name__}: {exc}", time.time() - started)
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                return last
            with self._lock:
                self.calls += 1
            reply = Reply(response.status_code, response.text, time.time() - started)
            if response.status_code == 401 and attempt == 0:
                self.token(force=True)
                continue
            if response.status_code >= 500 and attempt < self.retries:
                last = reply
                time.sleep(2**attempt)
                continue
            return reply
        return last


def short_error(reply: Reply) -> str:
    """HTTP status plus SAP's own message, when it gave one."""
    text = reply.text or ""
    message = ""
    try:
        message = json.loads(text)["error"]["message"]["value"]
    except Exception:
        found = re.search(r"<message[^>]*>(.*?)</message>", text, re.S)
        message = found.group(1) if found else text[:160]
    message = " ".join(message.split())[:160]
    return f"HTTP {reply.status}" + (f": {message}" if message else "")


def combine(*clauses: str | None) -> str | None:
    """AND the predicates, each in parentheses -- exactly as the pipeline does."""
    present = [c for c in clauses if c]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return " and ".join(f"({c})" for c in present)


# --------------------------------------------------------------------------
# $metadata
# --------------------------------------------------------------------------


@dataclass
class SetMeta:
    name: str
    service: str
    keys: tuple[str, ...]
    props: dict[str, str]  # property -> Edm type, in metadata order

    @property
    def path(self) -> str:
        return f"sap/opu/odata/sap/{self.service}/{self.name}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def fetch_metadata(cpi: Cpi, services: tuple[str, ...]) -> tuple[dict[str, SetMeta], dict[str, str]]:
    """Every entity set of every service, from live $metadata."""
    sets: dict[str, SetMeta] = {}
    errors: dict[str, str] = {}
    for service in services:
        reply = cpi.get(f"sap/opu/odata/sap/{service}/$metadata", "")
        if not reply.ok:
            errors[service] = short_error(reply)
            continue
        try:
            root = ET.fromstring(reply.text)
        except ET.ParseError as exc:
            errors[service] = f"metadata is not XML: {exc}"
            continue
        types: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {}
        for node in root.iter():
            if _local(node.tag) != "EntityType":
                continue
            keys: list[str] = []
            props: dict[str, str] = {}
            for child in node:
                if _local(child.tag) == "Key":
                    keys = [ref.get("Name", "") for ref in child if _local(ref.tag) == "PropertyRef"]
                elif _local(child.tag) == "Property":
                    props[child.get("Name", "")] = child.get("Type", "")
            types[node.get("Name", "")] = (tuple(keys), props)
        for node in root.iter():
            if _local(node.tag) != "EntitySet":
                continue
            type_name = (node.get("EntityType") or "").rsplit(".", 1)[-1]
            if type_name in types:
                keys, props = types[type_name]
                sets[node.get("Name", "")] = SetMeta(node.get("Name", ""), service, keys, props)
    return sets, errors


# --------------------------------------------------------------------------
# What the initiatives need -- one entry per row of the data-requirements
# sheet. Fields are SAP technical names (what the CSV header carries); the
# OData property is found by name ignoring case and underscores, so MATNR
# matches Matnr and BUDAT_MKPF matches BudatMkpf.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Table:
    sap: str
    entity_set: str
    initiatives: tuple[str, ...]  # who reads the table at all (the sheet's tick columns)
    csv_keys: tuple[str, ...]
    fields: tuple[str, ...]  # every field checked, SAP names
    mandatory: dict[str, tuple[str, ...]] = field(default_factory=dict)  # field -> initiatives that list it
    notes: dict[str, str] = field(default_factory=dict)
    required_filter: str | None = None  # predicate SAP will not serve the set without
    count_filter: str | None = None  # what a CSV full extract reconciles against
    profile_all: bool = False  # no fixed list: check every property the service exposes
    descoped: str | None = None  # why the table is checked but not counted for an initiative

    @property
    def reference_filter(self) -> str | None:
        return self.count_filter or self.required_filter

    def users_of(self, sap_field: str) -> tuple[str, ...]:
        """Initiatives a field is mandatory for; empty = read by the views only."""
        return self.mandatory.get(sap_field, ())


def _t(sap, entity_set, initiatives, keys, fields, mandatory="", notes=None, **kw) -> Table:
    """``mandatory`` is "FIELD:I07,I13 FIELD:I08 ..." -- the FRS / sheet tags."""
    tags: dict[str, tuple[str, ...]] = {}
    for item in mandatory.split():
        name, _, who = item.partition(":")
        tags[name] = tuple(who.split(","))
    ordered = list(fields.split())
    ordered += [f for f in tags if f not in ordered]
    return Table(
        sap=sap,
        entity_set=entity_set,
        initiatives=tuple(initiatives.split()),
        csv_keys=tuple(keys.split()),
        fields=tuple(ordered),
        mandatory=tags,
        notes=notes or {},
        **kw,
    )


CDHDR_CLASSES = ("MATERIAL", "EINKBELEG", "KRED", "BANF", "INFOSATZ")

# Sources of the mandatory tags:
#   I13     -- Initiative 13 FRS v1.2 s7.1 "Required SAP data" (key fields per table)
#   I07/I08 -- the field list from the 29-Sep review (table / field / used in)
#   I08 s7  -- the exposure asks in I08 FRS s7 (SOBKZ, ERDAT, BEDNR)
# A field with no tag is read by the n_* views / initiative queries; it is
# still checked, just not counted as a mandatory gap.
CATALOGUE: tuple[Table, ...] = (
    _t("MARA", "MaterialSet", "I07 I08 I13", "MATNR",
       "MATNR MTART MATKL MEINS MSTAE LVORM EXTWG BISMT ERSDA ERNAM LAEDA SPART MFRNR",
       "MATNR:I07,I08 MSTAE:I07",
       {"MATNR": "80-series convention (I08)", "MTART": "spares scope",
        "EXTWG": "retired as OAR identifier (I13 s7.3); informational",
        "LAEDA": "change stamp for the weekly delta"}),
    _t("MAKT", "MaterialDescriptionSet", "I07 I08 I13", "MATNR SPRAS",
       "MATNR SPRAS MAKTX", "MATNR:I13 MAKTX:I07,I13",
       {"SPRAS": "English only (E)", "MAKTX": "FR-10 labels (I13)"},
       count_filter="Spras eq 'E'"),
    _t("MARC", "MaterialPlantSet", "I07 I08 I13", "MATNR WERKS",
       "MATNR WERKS LVORM DISMM DISPO PLIFZ WEBAZ MINBE EISBE BSTMI BSTMA MABST BESKZ ZZCRITIC",
       "MATNR:I13 WERKS:I13 DISMM:I07,I08,I13 PLIFZ:I07 MINBE:I07,I08 MABST:I07 EISBE:I07 LVORM:I07 DISPO:I07",
       {"DISMM": "THE OAR identifier: OAR = DISMM in (ND, PD)", "MINBE": "reorder point",
        "EISBE": "safety stock", "PLIFZ": "planned delivery time",
        "ZZCRITIC": "criticality candidate (W3.4)"}),
    _t("MARD", "StorageLocationStockSet", "I07 I08 I13", "MATNR WERKS LGORT",
       "MATNR WERKS LGORT LABST INSME SPEME RETME UMLME EINME LMINB LGPBE ERSDA",
       "MATNR:I13 WERKS:I13 LABST:I07,I08,I13",
       {"LABST": "unrestricted stock (SOH)"}),
    _t("MBEW", "MaterialValuationSet", "I07 I08 I13", "MATNR BWKEY BWTAR",
       "MATNR BWKEY BWTAR LBKUM SALK3 VPRSV VERPR STPRS PEINH BKLAS WAERS",
       "VERPR:I07 PEINH:I07 WAERS:I07",
       {"SALK3": "stock value", "VERPR": "moving price (I07 unit price)",
        "WAERS": "currency - not a MBEW column in SAP; only the OData projection can carry it (I07 FRS s7)"}),
    _t("MCHB", "BatchStockSet", "I07 I13", "MATNR WERKS LGORT CHARG",
       "MATNR WERKS LGORT CHARG CLABS", "MATNR:I13 WERKS:I13 CHARG:I13 CLABS:I08,I13",
       {"CLABS": "batch stock for batch-managed OAR materials (FR-1)"}),
    _t("MSEG", "GoodsMovementItemSet", "I07 I08 I13", "MBLNR MJAHR ZEILE",
       "MBLNR MJAHR ZEILE BWART MATNR WERKS LGORT CHARG SOBKZ LIFNR SHKZG DMBTR MENGE MEINS "
       "WAERS EBELN EBELP SGTXT WEMPF KOSTL AUFNR RSNUM RSPOS KZEAR KZVBR UMWRK UMLGO GRUND "
       "BUKRS ELIKZ BUDAT_MKPF CPUDT_MKPF XBLNR_MKPF",
       "MATNR:I13 WERKS:I13 BWART:I07,I13 MENGE:I13 MBLNR:I13 MJAHR:I13 EBELN:I13 EBELP:I13 "
       "RSNUM:I13 RSPOS:I13 BUDAT_MKPF:I07 SGTXT:I08 SOBKZ:I08",
       {"BWART": "201/261 consumption, 541 removal to repair", "BUDAT_MKPF": "posting date (joined from MKPF)",
        "SOBKZ": "special stock - without it 541 dispatch quantities double (I08 FRS s7)",
        "CPUDT_MKPF": "entry date - delta field on the sheet"}),
    _t("MKPF", "MaterialDocumentHeaderSet", "I07 I08 I13", "MBLNR MJAHR",
       "MBLNR MJAHR BUDAT CPUDT BLDAT XBLNR", "MBLNR:I13 MJAHR:I13 BUDAT:I13",
       {"BUDAT": "the single date basis for every movement metric; >= 731 days needed",
        "CPUDT": "entry date - delta field on the sheet"}),
    _t("EBAN", "PurchaseRequisitionSet", "I07 I08 I13", "BANFN BNFPO",
       "BANFN BNFPO BSART LOEKZ STATU FRGKZ FRGZU EKGRP ERNAM AFNAM TXZ01 MATNR WERKS LGORT "
       "BEDNR MATKL RESWK MENGE MEINS BADAT LFDAT PREIS PEINH FLIEF EBELN EBELP BSMNG EBAKZ "
       "RSNUM PSTYP KZVBR SOBKZ LIFNR WAERS PLIFZ",
       "BANFN:I13 BNFPO:I13 MATNR:I13 WERKS:I13 MENGE:I13 BADAT:I13 EBELN:I13 EBELP:I13 TXZ01:I08",
       {"BSART": "agreed PR BSART list", "BADAT": "requisition date - select on this, never AEDAT",
        "BEDNR": "session identifier carrier (I08)"}),
    _t("RESB", "ReservationItemSet", "I07 I08 I13", "RSNUM RSPOS",
       "RSNUM RSPOS XLOEK KZEAR MATNR WERKS LGORT CHARG SOBKZ BDTER BDMNG MEINS SHKZG ENMNG "
       "ENWRT WAERS BANFN BNFPO AUFNR BWART UMWRK UMLGO POSTP SGTXT EBELN EBELP EKGRP WEMPF "
       "MATKL LIFNR ABLAD ZZXBLNR BEDNR ZZAISESSION",
       "RSNUM:I13 RSPOS:I13 MATNR:I13 WERKS:I13 LGORT:I13 BDTER:I13 BDMNG:I13 MEINS:I13 ENMNG:I13 "
       "ENWRT:I13 BANFN:I13 BNFPO:I13 AUFNR:I13 BWART:I13 UMWRK:I13 UMLGO:I13 WEMPF:I13 XLOEK:I13 "
       "KZEAR:I13 ZZAISESSION:I13 BEDNR:I08,I13",
       {"XLOEK": "blank = not deleted (open demand)", "KZEAR": "blank = not final issue",
        "WEMPF": "requester proxy", "AUFNR": "next stitching key (I13 s7.5)",
        "ZZAISESSION": "session identifier - REQUIRED AND ABSENT (I13 s7.2)",
        "BEDNR": "designated session carrier - asked for in I08 FRS s7",
        "ZZXBLNR": "session-ID read-back candidate"}),
    _t("EKKO", "PurchaseOrderSet", "I07 I08 I13", "EBELN",
       "EBELN BUKRS BSART LOEKZ AEDAT BEDAT ERNAM LIFNR EKORG EKGRP WAERS",
       "AEDAT:I07 BSART:I08",
       {"BSART": "repair-PO identification (D7)", "AEDAT": "delta field; interim release-date proxy (I11)",
        "BEDAT": "PO date"}),
    _t("EKPO", "PurchaseOrderItemSet", "I07 I08 I13", "EBELN EBELP",
       "EBELN EBELP LOEKZ AEDAT ERDAT TXZ01 MATNR BUKRS WERKS LGORT BEDNR MATKL MENGE MEINS NETPR "
       "PEINH NETWR ELIKZ PSTYP KNTTP KZVBR PLIFZ SOBKZ BANFN BNFPO MTART AFNAM CREATIONDATE",
       "EBELN:I13 EBELP:I13 BANFN:I13 BNFPO:I13 MATNR:I13 WERKS:I13 MENGE:I13 NETPR:I07 TXZ01:I08 PSTYP:I08",
       {"LOEKZ": "exclude deleted lines", "PSTYP": "item category - repair PO (I08)",
        "BANFN": "PR-to-PO stitching key", "ERDAT": "line creation date - asked for in I08 FRS s7",
        "ELIKZ": "delivery completed - open-PO coverage"}),
    _t("EKET", "POScheduleLineSet", "I07 I08 I13", "EBELN EBELP ETENR",
       "EBELN EBELP ETENR EINDT MENGE WEMNG WAMNG BANFN BNFPO RSNUM CHARG",
       "EBELN:I13 EBELP:I13 EINDT:I07,I08,I13 MENGE:I13",
       {"EINDT": "expected delivery date", "WEMNG": "delivered qty (open = MENGE - WEMNG)"}),
    _t("EKBE", "POHistorySet", "I07 I08 I13", "EBELN EBELP ZEKKN VGABE GJAHR BELNR BUZEI",
       "EBELN EBELP ZEKKN VGABE GJAHR BELNR BUZEI BEWTP BWART BUDAT MENGE DMBTR WAERS SHKZG "
       "CPUDT MATNR WERKS ELIKZ XBLNR CHARG BLDAT ERNAM",
       "EBELN:I13 EBELP:I13 VGABE:I07,I08,I13 BWART:I13 MENGE:I13 BUDAT:I13",
       {"VGABE": "1 = GR, 2 = IR; I13 filters VGABE = E", "BUDAT": "GR date - I11 lead time",
        "CPUDT": "entry date - delta field on the sheet"}),
    _t("EINA", "InfoRecordSet", "I07 I08", "INFNR",
       "INFNR MATNR LIFNR LOEKZ", notes={"LOEKZ": "active only (blank)"}),
    _t("EINE", "InfoRecordOrgSet", "I07 I08", "INFNR EKORG ESOKZ WERKS",
       "INFNR EKORG ESOKZ WERKS LOEKZ WAERS APLFZ NETPR PEINH EFFPR",
       notes={"NETPR": "benchmark price", "APLFZ": "lead time - reference only"}),
    _t("LFA1", "VendorSet", "I08", "LIFNR",
       "LIFNR NAME1 KTOKK LOEVM LAND1 ORT01",
       notes={"NAME1": "repair-vendor name (business fields only, POPIA)"}),
    _t("CDHDR", "ChangeDocHeaderSet", "I07 I08 I13", "OBJECTCLAS OBJECTID CHANGENR",
       "OBJECTCLAS OBJECTID CHANGENR USERNAME UDATE UTIME TCODE CHANGE_IND",
       notes={"UDATE": "delta field", "OBJECTCLAS": "BANF (PR), MATERIAL (MRP audit)"},
       required_filter="Objectclas eq 'MATERIAL'",
       count_filter=" or ".join(f"Objectclas eq '{c}'" for c in CDHDR_CLASSES)),
    _t("CDPOS", "ChangeDocItemSet", "I07 I08 I13",
       "OBJECTCLAS OBJECTID CHANGENR TABNAME TABKEY FNAME CHNGIND",
       "OBJECTCLAS OBJECTID CHANGENR TABNAME TABKEY FNAME CHNGIND VALUE_NEW VALUE_OLD",
       "FNAME:I07",
       {"FNAME": "tracked: DISMM EISBE MINBE MABST (I07 adoption)"},
       required_filter="Objectclas eq 'MATERIAL'"),
    _t("S031", "MonthlyMovementStatisticSet", "I07 I13",
       "SSOUR VRSIO SPMON SPTAG SPWOC SPBUP WERKS MATNR LGORT",
       "SPMON WERKS MATNR LGORT BASME MZUBB WZUBB MAGBB WAGBB AMBWG MUVBR WUVBR",
       notes={"MUVBR": "unplanned consumption qty"}),
    _t("S032", "StockMovementStatisticSet", "I07 I13", "SSOUR VRSIO WERKS LGORT MATNR",
       "WERKS LGORT MATNR DISPO MTART MATKL DISMM MBWBEST WBWBEST LETZTZUG LETZTABG LETZTVER "
       "LETZTBEW EISBE",
       notes={"LETZTBEW": "last movement - NM/SMI aging", "LETZTVER": "last consumption"}),
    # Gate pass: on the service list, descoped from I08 on 15-Aug. No agreed
    # field list, so every property the service exposes is profiled, and the
    # tables are not counted against any initiative.
    _t("ZMM_GP_HDR", "GatePassHeaderSet", "", "", "", profile_all=True,
       descoped="gate pass - descoped from I08 on 15-Aug; checked, not counted"),
    _t("ZMM_GP_ITEM", "GatePassItemSet", "", "", "", profile_all=True,
       descoped="gate pass - descoped from I08 on 15-Aug; checked, not counted"),
    _t("ZMM_GP_IN", "GatePassReturnSet", "", "", "", profile_all=True,
       descoped="gate pass return - descoped from I08 on 15-Aug; checked, not counted"),
)

BY_SAP = {t.sap: t for t in CATALOGUE}
INITIATIVES = ("I07", "I08", "I13")


def fields_to_check(table: Table, meta: SetMeta | None) -> tuple[str, ...]:
    """The catalogue's list, or -- for a profile_all table -- every live property."""
    if table.profile_all and meta is not None:
        return tuple(p.upper() for p in meta.props)
    return table.fields


DATE_FIELDS = frozenset(
    "AEDAT BEDAT BUDAT BUDAT_MKPF CPUDT CPUDT_MKPF BLDAT BADAT LFDAT UDATE EINDT BDTER ERSDA "
    "ERDAT LAEDA CREATIONDATE LETZTZUG LETZTABG LETZTVER LETZTBEW".split()
)
NUMERIC_FIELDS = frozenset(
    "MENGE DMBTR LABST INSME SPEME RETME UMLME EINME LMINB MINBE EISBE BSTMI BSTMA MABST PLIFZ "
    "WEBAZ LBKUM SALK3 VERPR STPRS PEINH CLABS NETPR NETWR BDMNG ENMNG ENWRT WEMNG WAMNG APLFZ "
    "EFFPR PREIS BSMNG MBWBEST WBWBEST MZUBB WZUBB MAGBB WAGBB AMBWG MUVBR WUVBR".split()
)
PLANT_FIELDS = ("WERKS", "BWKEY")


def norm(name: str) -> str:
    """MATNR, Matnr and BUDAT_MKPF / BudatMkpf compare equal."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def odata_property(meta: SetMeta, sap_field: str) -> str | None:
    wanted = norm(sap_field)
    for prop in meta.props:
        if norm(prop) == wanted:
            return prop
    return None


def norm_key(value: Any) -> str:
    """Keys compared across routes: trimmed, upper-case, leading zeros dropped."""
    text = str(value if value is not None else "").strip().upper()
    return text.lstrip("0") or ("0" if text else "")


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

_SAP_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def sap_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    match = _SAP_DATE.match(raw)
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def nearest_day(moment: datetime) -> date:
    """SAP serialises a DATS as local midnight: 2013-09-27 arrives as
    2013-09-26T22:00Z. Rounding to the nearest day recovers the real date."""
    return (moment + timedelta(hours=12)).date()


def count(cpi: Cpi, meta: SetMeta, filter: str | None = None) -> tuple[int | None, str | None]:
    reply = cpi.get(f"{meta.path}/$count", f"$filter={filter}" if filter else "")
    if not reply.ok:
        return None, short_error(reply)
    try:
        return int(reply.text.strip()), None
    except ValueError:
        return None, f"$count answered {reply.text[:60]!r}"


def read_page(
    cpi: Cpi, meta: SetMeta, *, filter: str | None, order_by: tuple[str, ...], top: int, skip: int
) -> tuple[list[dict] | None, str | None]:
    parts = []
    if filter:
        parts.append(f"$filter={filter}")
    if order_by:
        parts.append(f"$orderby={','.join(order_by)}")
    parts += [f"$top={top}", f"$skip={skip}", "$format=json"]
    reply = cpi.get(meta.path, "&".join(parts))
    if not reply.ok:
        return None, short_error(reply)
    try:
        inner = json.loads(reply.text)["d"]
    except Exception:
        return None, f"not an OData JSON envelope: {reply.text[:120]!r}"
    rows = inner.get("results", [inner]) if isinstance(inner, dict) else []
    return [r for r in rows if isinstance(r, dict)], None


@dataclass
class ReadOut:
    rows: list[dict] = field(default_factory=list)
    total: int | None = None
    count_error: str | None = None
    order_by: tuple[str, ...] = ()
    degraded: bool = False
    pages: int = 0
    duplicates: int = 0
    sampled: bool = False
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.error is None and not self.sampled and (self.total is None or len(self.rows) == self.total)


def key_tuple(row: dict, keys: tuple[str, ...]) -> tuple:
    return tuple(row.get(k) for k in keys)


def read_all(
    cpi: Cpi, meta: SetMeta, *, filter: str | None = None, limit: int | None = None, page_size: int = 1000,
    total: tuple[int | None, str | None] | None = None,
) -> ReadOut:
    """Key-ordered paging, negotiating the longest $orderby SAP accepts
    (MaterialPlantSet 500s on any two-field ordering). Stops at a short page,
    at $count, or at ``limit`` (then marked as a sample)."""
    out = ReadOut()
    out.total, out.count_error = total if total is not None else count(cpi, meta, filter)
    size = min(page_size, limit) if limit else page_size
    first: list[dict] | None = None
    last_error = None
    for length in range(len(meta.keys), 0, -1):
        order = meta.keys[:length]
        first, last_error = read_page(cpi, meta, filter=filter, order_by=order, top=size, skip=0)
        if first is not None:
            out.order_by, out.degraded = order, length < len(meta.keys)
            break
    if first is None:
        out.error = last_error or "no key to order by"
        return out
    page = first
    while True:
        out.pages += 1
        out.rows.extend(page)
        if len(page) < size:
            break
        if out.total is not None and len(out.rows) >= out.total:
            break
        if limit and len(out.rows) >= limit:
            out.sampled = out.total is None or len(out.rows) < out.total
            break
        page, error = read_page(cpi, meta, filter=filter, order_by=out.order_by, top=size, skip=len(out.rows))
        if page is None:
            out.error = f"page at skip={len(out.rows)}: {error}"
            break
        if not page:
            break
    out.duplicates = len(out.rows) - len({key_tuple(r, meta.keys) for r in out.rows})
    return out


# --------------------------------------------------------------------------
# Field profiling
# --------------------------------------------------------------------------


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    negative = text.endswith("-")
    try:
        number = float(text.rstrip("-"))
    except ValueError:
        return None
    return -number if negative else number


def profile_odata(table: Table, meta: SetMeta, rows: list[dict]) -> dict[str, dict]:
    """Per required field: projected?, type, fill %, non-zero %, date range."""
    result: dict[str, dict] = {}
    n = len(rows)
    for sap_field in fields_to_check(table, meta):
        prop = odata_property(meta, sap_field)
        if prop is None:
            result[sap_field] = {"odata": None}
            continue
        edm = meta.props[prop]
        filled = nonzero = parsed = late = 0
        low: date | None = None
        high: date | None = None
        for row in rows:
            value = row.get(prop)
            if _blank(value):
                continue
            filled += 1
            if edm in ("Edm.DateTime", "Edm.DateTimeOffset"):
                moment = sap_datetime(value)
                if moment is None:
                    continue
                parsed += 1
                if moment.hour >= 22:
                    late += 1
                day = nearest_day(moment)
                low = day if low is None or day < low else low
                high = day if high is None or day > high else high
            elif sap_field in NUMERIC_FIELDS or edm in ("Edm.Decimal", "Edm.Double", "Edm.Int32", "Edm.Int16"):
                number = _number(value)
                if number is not None:
                    parsed += 1
                    nonzero += number != 0
            elif edm == "Edm.Boolean":
                nonzero += value is True or str(value).upper() in ("TRUE", "X")
        info: dict[str, Any] = {
            "odata": prop,
            "type": edm.replace("Edm.", ""),
            "fill": round(100 * filled / n, 1) if n else None,
        }
        if sap_field in NUMERIC_FIELDS or edm == "Edm.Decimal":
            info["nonzero"] = round(100 * nonzero / n, 1) if n else None
        if edm == "Edm.Boolean":
            info["true"] = round(100 * nonzero / n, 1) if n else None
        if low:
            info["range"] = f"{low}..{high}"
            info["at_22_utc"] = late
        result[sap_field] = info
    return result


def plant_spread(rows: list[dict], meta: SetMeta) -> dict[str, int]:
    for sap_field in PLANT_FIELDS:
        prop = odata_property(meta, sap_field)
        if prop:
            return dict(Counter(str(r.get(prop) or "").strip() or "(blank)" for r in rows).most_common(8))
    return {}


# --------------------------------------------------------------------------
# Delta probes -- mirrors of the pipeline's delta logic plus the sheet's asks
# --------------------------------------------------------------------------

# (entity set, field, predicate SAP demands, why it is probed)
DIRECT_PROBES: tuple[tuple[str, str, str | None, str], ...] = (
    ("PurchaseOrderSet", "Aedat", None, "pipeline delta for EKKO (runs hourly)"),
    ("PurchaseOrderSet", "Bedat", None, "sheet: PO date alternative"),
    ("ChangeDocHeaderSet", "Udate", "Objectclas eq 'MATERIAL'", "pipeline delta for CDHDR (MATERIAL)"),
    ("ChangeDocHeaderSet", "Udate", "Objectclas eq 'BANF'", "sheet: PR change-doc delta"),
    ("ChangeDocHeaderSet", "Udate", "Objectclas eq 'EINKBELEG'", "sheet: PO change-doc delta"),
    ("MaterialDocumentHeaderSet", "Budat", None, "pipeline: measured IGNORED 25-Sep"),
    ("MaterialDocumentHeaderSet", "Cpudt", None, "sheet: MKPF delta on CPUDT"),
    ("GoodsMovementItemSet", "CpudtMkpf", None, "sheet: MSEG delta on CPUDT_MKPF"),
    ("GoodsMovementItemSet", "BudatMkpf", None, "sheet: MSEG posting date"),
    ("PurchaseRequisitionSet", "Badat", None, "sheet: EBAN delta on BADAT"),
    ("PurchaseOrderItemSet", "Aedat", None, "EKPO own change date"),
    ("POHistorySet", "Cpudt", None, "sheet: EKBE delta on CPUDT"),
    ("ReservationItemSet", "Bdter", None, "sheet: RESB delta field TBC"),
)

# (child, key, parent, parent's window field, predicate, why)
DERIVED_PROBES: tuple[tuple[str, str, str, str | None, str | None, str], ...] = (
    ("PurchaseOrderItemSet", "Ebeln", "PurchaseOrderSet", "Aedat", None, "pipeline: EKPO by EBELN (runs)"),
    ("POScheduleLineSet", "Ebeln", "PurchaseOrderSet", "Aedat", None, "pipeline: EKET by EBELN (blocked)"),
    ("POHistorySet", "Ebeln", "PurchaseOrderSet", "Aedat", None, "pipeline: EKBE by EBELN (blocked)"),
    ("GoodsMovementItemSet", "Mblnr", "MaterialDocumentHeaderSet", None, None, "pipeline: MSEG by MBLNR (declared)"),
    ("ChangeDocItemSet", "Changenr", "ChangeDocHeaderSet", "Udate", "Objectclas eq 'MATERIAL'", "sheet: CDPOS by CHANGENR"),
)

KEY_BATCH = 50  # the pipeline's batch size for parent keys


def _string_date_format(samples: list[Any]) -> str | None:
    for value in samples:
        text = str(value or "").strip()
        if not text or set(text) <= {"0"}:
            continue
        if re.fullmatch(r"\d{8}", text):
            return "%Y%m%d"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return "%Y-%m-%d"
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", text):
            return "%d.%m.%Y"
        return None
    return None


def literal_for(meta: SetMeta, prop: str, sample: list[dict]):
    """How to write a date for this property in a $filter, from its LIVE type
    and the format its values actually arrive in.

    Returns (builder, rangeable, real_day, why_not). A DateTime takes
    datetime'YYYY-MM-DDT00:00:00'; a String takes the quoted text in its own
    format. dd.mm.yyyy strings can be matched with eq but not ordered, so a
    ge window on them is meaningless -- rangeable is False.
    """
    edm = meta.props.get(prop)
    if edm is None:
        return None, False, None, "NOT PROJECTED"
    if edm in ("Edm.DateTime", "Edm.DateTimeOffset"):
        real_day = None
        for row in sample:
            moment = sap_datetime(row.get(prop))
            if moment and moment.year > 1900:
                real_day = nearest_day(moment)
                break
        return (lambda day: f"datetime'{day:%Y-%m-%d}T00:00:00'"), True, real_day, None
    fmt = _string_date_format([row.get(prop) for row in sample])
    if fmt is None:
        return None, False, None, "NOT A DATE (no usable sample value)"
    real_day = None
    for row in sample:
        text = str(row.get(prop) or "").strip()
        if text and set(text) > {"0"}:
            try:
                real_day = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
    if fmt == "%d.%m.%Y":
        # Shown as dd.mm.yyyy but filtered on SAP's internal DATS form:
        # measured 29-Sep, Badat eq '20190530' -> 2 and ge '20260101' -> 79,
        # while eq '30.05.2019' matches nothing.
        return (lambda day: f"'{day:%Y%m%d}'"), True, real_day, None
    return (lambda day: f"'{day.strftime(fmt)}'"), True, real_day, None


def probe_direct(
    cpi: Cpi, meta: SetMeta, prop: str, predicate: str | None, since: date, sample: list[dict]
) -> dict[str, Any]:
    """Total / impossible / one real day / window. Every number a $count."""
    edm = meta.props.get(prop)
    result: dict[str, Any] = {"set": meta.name, "field": prop, "predicate": predicate, "type": edm}
    literal, rangeable, real_day, why_not = literal_for(meta, prop, sample)
    if literal is None:
        result["verdict"] = why_not
        return result
    if edm == "Edm.String":
        example = next((str(r.get(prop)) for r in sample if r.get(prop)), "")
        result["format"] = f"string like {example!r}, filtered as {literal(date(2026, 1, 2))}"

    total, error = count(cpi, meta, predicate)
    if total is None:
        result.update(verdict="UNKNOWN", error=f"$count: {error}")
        return result
    impossible, e1 = count(cpi, meta, combine(predicate, f"{prop} eq {literal(date(1900, 1, 1))}"))
    window, e2 = (None, None)
    if rangeable:
        window, e2 = count(cpi, meta, combine(predicate, f"{prop} ge {literal(since)}"))
    control, e3 = (None, "no real value in the sample")
    if real_day:
        control, e3 = count(cpi, meta, combine(predicate, f"{prop} eq {literal(real_day)}"))
    result.update(total=total, impossible=impossible, window=window, since=str(since),
                  real_day=str(real_day) if real_day else None, control=control)

    errors = [e for e in (e1, e2) if e]
    if errors:
        result.update(verdict="REJECTED", error=errors[0])
    elif total == 0:
        result["verdict"] = "EMPTY SET (cannot tell)"
    elif impossible == total:
        result["verdict"] = "IGNORED"
    elif control is not None and total > 1 and control == total:
        result["verdict"] = "IGNORED"
    elif impossible == 0 and control is not None and 0 < control < total:
        if not rangeable:
            result["verdict"] = "EQ ONLY"
            result["note"] = "dd.mm.yyyy string: eq per day works, ge/lt cannot order it"
        elif window is not None and window <= total:
            result["verdict"] = "HONOURED"
        else:
            result["verdict"] = "ODD"
    elif impossible == 0 and control is None:
        result.update(verdict="PROBABLY HONOURED", error=f"control not run: {e3}")
    else:
        result["verdict"] = "ODD"
    return result


def probe_derived(
    cpi: Cpi, child: SetMeta, key: str, parent: SetMeta, window_field: str | None,
    predicate: str | None, since: date,
) -> dict[str, Any]:
    """Read the parent's window, then the child for those keys; every child
    row must carry one of the keys, or the key filter was ignored."""
    result: dict[str, Any] = {"child": child.name, "key": key, "parent": parent.name}
    parent_key = next((k for k in parent.keys if norm(k) == norm(key)), None) or key
    window = predicate
    if window_field and window_field in parent.props:
        # Written for the field's LIVE type: CDHDR's Udate is a String on the
        # live service, and a datetime literal against it is HTTP 400.
        head, _ = read_page(cpi, parent, filter=predicate, order_by=(), top=20, skip=0)
        literal, rangeable, _, _ = literal_for(parent, window_field, head or [])
        if literal and rangeable:
            window = combine(predicate, f"{window_field} ge {literal(since)}")
    rows, error = read_page(cpi, parent, filter=window, order_by=parent.keys[:1], top=KEY_BATCH * 4, skip=0)
    if rows is None:
        result.update(verdict="PARENT FAILED", error=error)
        return result
    keys = list(dict.fromkeys(str(r.get(parent_key) or "") for r in rows if r.get(parent_key)))[:KEY_BATCH]
    result["parent_window"] = window
    result["keys_used"] = len(keys)
    if not keys:
        result["verdict"] = "NO PARENT KEYS in the window"
        return result

    def fetch(batch: list[str]) -> tuple[list[dict] | None, str | None]:
        clause = combine(predicate, " or ".join(f"{key} eq '{k}'" for k in batch))
        got: list[dict] = []
        skip = 0
        order = child.keys
        while True:
            page, err = read_page(cpi, child, filter=clause, order_by=order, top=1000, skip=skip)
            if page is None and skip == 0 and len(order) > 1:
                order = child.keys[:1]  # same $orderby negotiation as a full read
                page, err = read_page(cpi, child, filter=clause, order_by=order, top=1000, skip=skip)
            if page is None:
                return None, err
            got.extend(page)
            if len(page) < 1000:
                return got, None
            skip += len(page)

    batch = keys if child.name != "ChangeDocItemSet" else keys[:5]
    got, error = fetch(batch)
    result["batch"] = len(batch)
    if got is None and child.name == "ChangeDocItemSet":
        # The pipeline's or-chain is measured REJECTED here; one key at a time
        # is the only shape left, so say whether that works.
        single, single_error = fetch(keys[:1])
        result["batch_error"] = error
        if single is None:
            result.update(verdict="REJECTED", error=single_error)
            return result
        got, batch = single, keys[:1]
        result["note"] = "or-chain rejected; one key per request works"
    if got is None:
        result.update(verdict="REJECTED", error=error)
        return result
    wanted = {norm_key(k) for k in batch}
    child_key = next((p for p in child.props if norm(p) == norm(key)), key)
    outside = [r for r in got if norm_key(r.get(child_key)) not in wanted]
    matched = {norm_key(r.get(child_key)) for r in got} & wanted
    result.update(rows=len(got), parents_matched=len(matched), rows_outside_keys=len(outside))
    if outside:
        result["verdict"] = "IGNORED (rows for other keys came back)"
    elif not got:
        result["verdict"] = "EMPTY (no child rows for these keys)"
    else:
        result["verdict"] = "HONOURED"
    return result


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def default_out(name: str) -> Path:
    """/home/live_checks on App Service (persistent), else the temp dir.
    Never inside the repository: the report describes real SAP data."""
    home = Path("/home")
    folder = home / "live_checks" if home.is_dir() and os.access(home, os.W_OK) else Path(tempfile.gettempdir()) / "live_checks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.json"


def fmt_n(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else ("-" if value is None else str(value))


def print_table_report(table: Table, entry: dict) -> None:
    who = " ".join(table.initiatives) or (table.descoped or "-")
    head = f"{table.sap:<11} {table.entity_set:<28} [{entry.get('service', '?')}]  {who}"
    print("\n" + "=" * len(head) + "\n" + head + "\n" + "=" * len(head))
    if entry.get("error_meta"):
        print(f"  metadata   FAIL  {entry['error_meta']}")
        return
    fields = entry["fields"]
    missing = [f for f, i in fields.items() if i.get("odata") is None]
    print(f"  metadata   ok    keys={','.join(entry['keys'])}  checked {len(fields)}: "
          f"{len(fields) - len(missing)} projected, {len(missing)} not in OData")
    print(f"  $count     {fmt_n(entry.get('count')) if entry.get('count') is not None else 'unavailable (' + str(entry.get('count_error')) + ')'}")
    read = entry["read"]
    if read.get("error"):
        print(f"  read       FAIL  {read['error']}  (after {fmt_n(read['rows'])} rows)")
    else:
        verdict = "SAMPLE" if read["sampled"] else ("COMPLETE" if read["complete"] else "SHORT")
        order = ",".join(read["order_by"]) + (" (degraded - not the full key)" if read["degraded"] else "")
        print(f"  read       {fmt_n(read['rows'])} rows / {read['pages']} page(s) in {read.get('seconds', 0):.0f}s, "
              f"ordered by {order}, {read['duplicates']} duplicate key(s) -> {verdict}")
        if read["duplicates"] and read["pages"] == 1:
            print(f"             one page, so these are not paging losses: the declared key "
                  f"{','.join(entry['keys'])} does not address a row uniquely (a full paged read by it can lose rows)")
    print(f"  {'field':<12} {'needed by':<12} {'odata':<12} {'type':<9} {'fill':>6} {'nonzero':>8}  notes")
    for sap_field, info in fields.items():
        note = table.notes.get(sap_field, "")
        needed = ",".join(table.users_of(sap_field)) or "views"
        if info.get("odata") is None:
            mark = "MANDATORY GAP" if table.users_of(sap_field) else "not in OData"
            print(f"  {sap_field:<12} {needed:<12} {'--':<12} {'':<9} {'':>6} {'':>8}  {mark} {note}")
            continue
        fill = "-" if info.get("fill") is None else ("<1%" if 0 < info["fill"] < 1 else f"{info['fill']:.0f}%")
        nonzero = info.get("nonzero", info.get("true"))
        nz = "" if nonzero is None else f"{nonzero:.0f}%"
        extra = f"{info['range']}" if info.get("range") else ""
        if info.get("at_22_utc"):
            extra += f" ({info['at_22_utc']:,} at 22:00Z+, rounded)"
        flag = "  <- EMPTY" if info.get("fill") == 0 else ""
        print(f"  {sap_field:<12} {needed:<12} {info['odata']:<12} {info['type']:<9} {fill:>6} {nz:>8}  "
              f"{' '.join(x for x in (note, extra) if x)}{flag}")
    if entry.get("plants"):
        print("  plants     " + "  ".join(f"{k}: {v:,}" for k, v in entry["plants"].items()))


def check_table(cpi: Cpi, table: Table, metas: dict[str, SetMeta], *, quick: bool, max_full_rows: int,
                page_size: int) -> dict[str, Any]:
    """Everything for one table: metadata, count, read, profile. Thread-safe."""
    entry: dict[str, Any] = {"entity_set": table.entity_set, "initiatives": table.initiatives,
                             "descoped": table.descoped}
    meta = metas.get(table.entity_set)
    if meta is None:
        entry["error_meta"] = f"{table.entity_set} is not in the $metadata of any service checked"
        entry["fields"] = {f: {"odata": None} for f in table.fields}
        return entry
    started = time.time()
    entry.update(service=meta.service, keys=list(meta.keys))
    counted = count(cpi, meta, table.required_filter)
    entry.update(count=counted[0], count_error=counted[1])
    limit = 1000 if quick else None
    if not quick and counted[0] is not None and counted[0] > max_full_rows:
        limit = 5000
    read = read_all(cpi, meta, filter=table.required_filter, limit=limit, page_size=page_size, total=counted)
    entry["read"] = {"rows": len(read.rows), "pages": read.pages, "order_by": list(read.order_by),
                     "degraded": read.degraded, "duplicates": read.duplicates, "sampled": read.sampled,
                     "complete": read.complete, "error": read.error, "filter": table.required_filter,
                     "seconds": round(time.time() - started, 1)}
    entry["fields"] = profile_odata(table, meta, read.rows)
    entry["plants"] = plant_spread(read.rows, meta)
    entry["extra_properties"] = [p for p in meta.props if not any(norm(p) == norm(f) for f in table.fields)]
    entry["_rows"] = read.rows  # for the delta probes; stripped before the report is written
    return entry


def coverage_odata(report: dict) -> None:
    """Per initiative: the mandatory fields (FRS / sheet) and whether OData
    serves them with data. The CSV check adds the other route."""
    print("\nINITIATIVE COVERAGE -- OData route only. Run csv_live_check.py --odata-report for both routes.")
    for initiative in INITIATIVES:
        mandatory = gaps = empty = unread = 0
        other = other_ok = 0
        gap_list: list[str] = []
        for sap, entry in report["tables"].items():
            table = BY_SAP[sap]
            if table.descoped:
                continue
            unreadable = bool(entry.get("read", {}).get("error")) or bool(entry.get("error_meta"))
            for sap_field, info in entry["fields"].items():
                served = info.get("odata") is not None and (info.get("fill") or 0) > 0
                if initiative in table.users_of(sap_field):
                    mandatory += 1
                    if info.get("odata") is None:
                        gaps += 1
                        gap_list.append(f"{sap}.{sap_field}")
                    elif not served:
                        if unreadable:
                            unread += 1
                        else:
                            empty += 1
                        gap_list.append(f"{sap}.{sap_field}({'unreadable' if unreadable else 'empty'})")
                elif initiative in table.initiatives and not table.users_of(sap_field):
                    other += 1
                    other_ok += served
        verdict = "n/a (no mandatory field in this run)" if not mandatory else ("PASS" if not gap_list else "FAIL")
        print(f"  {initiative}: mandatory {mandatory}, served by OData with data {mandatory - gaps - empty - unread}, "
              f"not projected {gaps}, unreadable (HTTP 500) {unread}, empty {empty} -> {verdict}; "
              f"other fields the views read: {other_ok}/{other}")
        if gap_list:
            print("       " + " ".join(gap_list))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tables", nargs="+", metavar="SAP", help="e.g. EKKO EKPO (default: every table)")
    parser.add_argument("--quick", action="store_true", help="read a 1,000-row sample per set instead of every row")
    parser.add_argument("--max-full-rows", type=int, default=150_000,
                        help="sets larger than this are sampled, not read whole (default 150,000)")
    parser.add_argument("--page-size", type=int, default=1000, help="rows per page (default 1000)")
    parser.add_argument("--workers", type=int, default=3, help="tables read in parallel (default 3)")
    parser.add_argument("--since", default=None, help="delta window start, YYYY-MM-DD (default: 365 days ago)")
    parser.add_argument("--no-delta", action="store_true", help="skip the delta and derived probes")
    parser.add_argument("--services", nargs="+", default=list(DEFAULT_SERVICES))
    parser.add_argument("--env-file", help="read configuration from this .env instead of ./.env")
    parser.add_argument("--out", help="JSON report path (default: /home/live_checks/odata_<time>.json)")
    args = parser.parse_args(argv)

    wanted = [t.upper() for t in args.tables] if args.tables else [t.sap for t in CATALOGUE]
    unknown = [t for t in wanted if t not in BY_SAP]
    if unknown:
        parser.error(f"unknown table(s) {unknown}; known: {' '.join(BY_SAP)}")
    since = date.fromisoformat(args.since) if args.since else date.today() - timedelta(days=365)

    config = load_config(args.env_file)
    cpi = Cpi(config)
    started = time.time()
    print(f"OData live check  {datetime.now():%Y-%m-%d %H:%M}  endpoint {cpi.endpoint}")
    cpi.token()
    print("token      ok")
    metas, meta_errors = fetch_metadata(cpi, tuple(args.services))
    for service in args.services:
        if service in meta_errors:
            print(f"$metadata  {service}: FAIL {meta_errors[service]}")
        else:
            print(f"$metadata  {service}: {sum(1 for m in metas.values() if m.service == service)} entity sets")
    unlisted = sorted(set(metas) - {t.entity_set for t in CATALOGUE})
    if unlisted:
        print(f"           entity sets not in the catalogue: {', '.join(unlisted)}")

    report: dict[str, Any] = {
        "run": {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "endpoint": cpi.endpoint,
                "since": str(since), "quick": args.quick, "services": args.services,
                "metadata_errors": meta_errors, "unlisted_entity_sets": unlisted},
        "tables": {}, "delta": [], "derived": [],
    }

    print(f"\nREADING {len(wanted)} table(s), {args.workers} at a time ...")
    from concurrent.futures import ThreadPoolExecutor

    def run(sap: str) -> tuple[str, dict]:
        entry = check_table(cpi, BY_SAP[sap], metas, quick=args.quick, max_full_rows=args.max_full_rows,
                            page_size=args.page_size)
        read = entry.get("read", {})
        state = entry.get("error_meta") or read.get("error") or f"{read.get('rows', 0):,} rows in {read.get('seconds', 0):.0f}s"
        print(f"  done {sap:<11} {state}", flush=True)
        return sap, entry

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = dict(pool.map(run, wanted))

    failures = 0
    samples: dict[str, list[dict]] = {}
    for sap in wanted:
        entry = results[sap]
        samples[BY_SAP[sap].entity_set] = entry.pop("_rows", [])
        if entry.get("error_meta") or entry["read"].get("error") or entry["read"].get("duplicates"):
            failures += 1
        report["tables"][sap] = entry
        print_table_report(BY_SAP[sap], entry)

    if not args.no_delta:
        print(f"\nDELTA FILTERS  four-probe test: total / eq 1900-01-01 (must be 0) / eq one real day "
              f"(must be a subset) / ge {since}")
        print(f"  {'set.field':<34} {'predicate':<26} {'total':>8} {'1900':>7} {'day':>7} {'window':>8}  verdict")
        for set_name, prop, predicate, why in DIRECT_PROBES:
            meta = metas.get(set_name)
            if meta is None:
                continue
            # The real-day control must come from rows the probe's own
            # predicate selects, or a BANF probe would test a MATERIAL date.
            sample = None if predicate else samples.get(set_name)
            if not sample:
                sample, _ = read_page(cpi, meta, filter=predicate, order_by=meta.keys[:1], top=200, skip=0)
                sample = sample or []
            probe = probe_direct(cpi, meta, prop, predicate, since, sample)
            probe["why"] = why
            report["delta"].append(probe)
            label = f"{set_name}.{prop}"
            pred = (predicate or "").replace("Objectclas eq ", "class=")
            print(f"  {label:<34} {pred:<26} {fmt_n(probe.get('total')):>8} {fmt_n(probe.get('impossible')):>7} "
                  f"{fmt_n(probe.get('control')):>7} {fmt_n(probe.get('window')):>8}  {probe['verdict']}"
                  f"{'  ' + probe['error'] if probe.get('error') else ''}"
                  f"{'  (' + probe['format'] + ')' if probe.get('format') else ''}   [{why}]")

        print(f"\nDERIVED DELTAS  child read by up to {KEY_BATCH} parent keys from the parent's window")
        for child_name, key, parent_name, window_field, predicate, why in DERIVED_PROBES:
            child, parent = metas.get(child_name), metas.get(parent_name)
            if child is None or parent is None:
                continue
            probe = probe_derived(cpi, child, key, parent, window_field, predicate, since)
            probe["why"] = why
            report["derived"].append(probe)
            detail = (f"{probe.get('rows', 0):,} rows for {probe.get('parents_matched', 0)}/{probe.get('batch', 0)} keys, "
                      f"{probe.get('rows_outside_keys', 0)} outside" if "rows" in probe else probe.get("error", ""))
            print(f"  {child_name:<22} by {key:<9} from {parent_name:<26} -> {probe['verdict']:<10} {detail}"
                  f"{'  (' + probe['note'] + ')' if probe.get('note') else ''}   [{why}]")

    coverage_odata(report)

    report["run"].update(seconds=round(time.time() - started), cpi_calls=cpi.calls, failures=failures)
    out = Path(args.out) if args.out else default_out("odata")
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n{cpi.calls} CPI calls in {time.time() - started:.0f}s; {failures} table(s) with a failed read.")
    print(f"report: {out}")
    print("       (pass it to csv_live_check.py --odata-report for the combined coverage matrix)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
