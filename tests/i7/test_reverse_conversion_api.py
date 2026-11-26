"""The Min-Max -> OAR endpoints, over a calculated report standing in for the stored run (no database).

The stored report, the data source and the request lookup are FastAPI dependencies, so the real routes,
validation, error envelope and CSV response run unchanged while the database is replaced.
"""

import csv
import io
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from reverse_conversion_support import FakeSource, population

from app.api.i7.reverse_conversion import get_alternative_lookup, get_brief_lookup, get_reverse_report, get_reverse_source
from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion.metrics import resolve_anchor
from app.initiatives.i7.reverse_conversion.repository import MinMaxAlternative
from app.initiatives.i7.reverse_conversion.service import build_report, clear_report_cache
from app.main import app

BASE = "/api/v1/i7/reverse-conversion/recommendations"


ALTERNATIVES: dict = {}
"""What the fake alternative lookup returns: (material, plant) -> MinMaxAlternative. It records what it was asked for."""
ASKED: list = []

BRIEFS: dict = {}
"""What the fake request lookup returns: (material, plant) -> (id, status, pending_role)."""


@pytest.fixture
def client():
    clear_report_cache()
    BRIEFS.clear()
    ALTERNATIVES.clear()
    ASKED.clear()
    source = FakeSource(*population(), data_loaded_at=datetime(2026, 11, 23, tzinfo=timezone.utc))
    anchor = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=date(2026, 10, 1))
    report = replace(
        build_report(source, current_reverse_conversion_policy(), anchor),
        generated_at=datetime(2026, 11, 26, 2, 30, tzinfo=timezone.utc),
        run_id=7,
    )
    app.dependency_overrides[get_reverse_source] = lambda: source
    app.dependency_overrides[get_reverse_report] = lambda: report
    app.dependency_overrides[get_brief_lookup] = lambda: (lambda keys: {k: BRIEFS[k] for k in keys if k in BRIEFS})

    def alternatives(keys):
        ASKED.extend(keys)
        return {k: ALTERNATIVES[k] for k in keys if k in ALTERNATIVES}

    app.dependency_overrides[get_alternative_lookup] = lambda: alternatives
    yield TestClient(app)
    for dependency in (get_reverse_source, get_reverse_report, get_brief_lookup, get_alternative_lookup):
        app.dependency_overrides.pop(dependency, None)
    clear_report_cache()


def materials(response):
    return [item["material"] for item in response.json()["items"]]


# --- The list ------------------------------------------------------------------------------------


def test_the_list_returns_the_anchor_policy_summary_reconciliation_and_items(client):
    response = client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"anchor", "policy", "summary", "reconciliation", "items", "total", "page", "page_size"}
    assert body["anchor"]["month"] == "2026-10" and body["anchor"]["source"] == "configured"
    assert body["policy"]["version"] == "reverse-conversion-v2"
    assert body["policy"]["parameters"]["recommended_max_issues"] == 2
    assert body["policy"]["parameters"]["frequent_issues_above"] == 4
    assert body["policy"]["parameters"]["maximum_lead_time_days"] == 90
    assert body["total"] == 18 and body["page"] == 1 and body["page_size"] == 25


def test_the_summary_counts_the_four_categories_for_the_whole_population(client):
    summary = client.get(BASE, params={"category": "not_eligible"}).json()["summary"]  # a filter must not change it
    counts = {row["category"]: row["count"] for row in summary["by_category"]}
    assert counts == {"recommended_for_oar": 15, "not_eligible": 3}
    assert [row["label"] for row in summary["by_category"]] == ["Recommended for OAR", "Not Eligible"]
    assert {row["currency"] for row in summary["by_category"]} == {"ZAR"}


def test_the_reconciliation_block_says_the_numbers_add_up(client):
    reconciliation = client.get(BASE).json()["reconciliation"]
    assert reconciliation["source_population"] == 19 and reconciliation["excluded"] == 1
    assert reconciliation["categorised"] == 18 and reconciliation["duplicate_keys"] == 0
    assert reconciliation["reconciled"] is True
    assert {p["plant"]: (p["source_population"], p["categorised"]) for p in reconciliation["by_plant"]} == {"1300": (18, 17), "1500": (1, 1)}


def test_an_item_carries_the_documented_fields(client):
    item = next(i for i in client.get(BASE, params={"search": "REC2"}).json()["items"])
    assert item["category"] == "recommended_for_oar" and item["category_label"] == "Recommended for OAR"
    assert item["reason"] == "sparse_recurring_demand" and item["reason_label"] == "Sparse recurring demand"
    assert item["plant"] == "1300" and item["mrp_type"] == "VM" and item["criticality"] == "NORMAL"
    assert item["issues_latest_12m"] == 2 and item["months_with_consumption_12m"] == 2
    assert item["first_valid_movement"] == "2015-01" and item["last_valid_movement"] == "2026-09"
    assert item["months_since_last_movement"] == 1 and item["lifetime_months_with_use"] == 7
    assert item["lead_time_days"] == "30" and item["lead_time_bucket"] == "0-30d"
    assert item["current_stock"] == "500" and item["unit_price"] == "10" and item["stock_value"] == "5000"
    assert item["minbe"] == "3" and item["mabst"] == "9" and item["currency"] == "ZAR" and item["price_currency"] == "ZAR"
    assert item["explanation"] and item["decision_rule"] == 12


def test_missing_price_and_zero_stock_rows_are_returned_not_dropped(client):
    items = {i["material"]: i for i in client.get(BASE, params={"category": "recommended_for_oar"}).json()["items"]}
    assert items["REC3"]["unit_price"] is None and items["REC3"]["stock_value"] is None
    assert items["REC4"]["current_stock"] == "0"
    assert items["REC5"]["current_stock"] is None


def test_the_list_is_ranked_inside_each_category_by_stock_value(client):
    assert materials(client.get(BASE, params={"category": "recommended_for_oar", "confidence": "high"})) == ["REC2", "REC1", "REC3", "REC4", "REC5"]


# --- Filters ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"category": "not_eligible"}, {"PROT", "NOHIST", "FREQ"}),
        ({"reason": "obsolete_criticality"}, {"OBS"}),
        ({"reason": "never_moved"}, {"NEVER"}),
        ({"reason": "zero_use_24m"}, {"ZERO24"}),
        ({"plant": "1500"}, {"REC5"}),
        ({"reason": "lead_time"}, {"LONGLT", "NOLT"}),
        ({"reason": "inconsistent_movement"}, {"INCONS"}),
        ({"criticality": "unknown"}, {"UNKCRIT"}),
        ({"criticality": "CRITICAL"}, {"PROT"}),
        ({"lead_time_bucket": ">90d"}, {"LONGLT"}),
        ({"lead_time_bucket": "unknown"}, {"NOLT"}),
        ({"search": "border"}, {"BORDER"}),
        ({"category": "recommended_for_oar", "reason": "lapsed"}, {"LAPSED"}),
        ({"confidence": "low"}, {"UNKCRIT", "INCONS", "LONGLT", "NOLT"}),
    ],
)
def test_filters(client, params, expected):
    assert set(materials(client.get(BASE, params={**params, "page_size": 100}))) == expected


def test_total_is_the_filtered_count_before_paging(client):
    body = client.get(BASE, params={"category": "recommended_for_oar", "page_size": 3}).json()
    assert body["total"] == 15 and len(body["items"]) == 3


@pytest.mark.parametrize(
    "params",
    [{"plant": "1200"}, {"reason": "nonsense"}, {"criticality": "SUPER"}],
)
def test_an_invalid_filter_value_is_a_400_with_the_error_envelope(client, params):
    response = client.get(BASE, params=params)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"].startswith("REVERSE_") and error["message"]


@pytest.mark.parametrize("params", [{"category": "bogus"}, {"lead_time_bucket": "forever"}, {"page": 0}, {"page_size": 0}, {"page_size": 201}])
def test_a_value_outside_the_allowed_set_is_refused(client, params):
    assert client.get(BASE, params=params).status_code == 422


# --- Paging -------------------------------------------------------------------------------------------


def test_paging_walks_the_whole_ranked_list_without_gaps_or_repeats(client):
    seen: list[str] = []
    for page in (1, 2, 3, 4):
        body = client.get(BASE, params={"page": page, "page_size": 5}).json()
        assert body["page"] == page and body["page_size"] == 5
        seen += [item["material"] for item in body["items"]]
    assert len(seen) == len(set(seen)) == 18


def test_a_page_past_the_end_is_empty_but_still_reports_the_total(client):
    body = client.get(BASE, params={"page": 99}).json()
    assert body["items"] == [] and body["total"] == 18


# --- The stored run -------------------------------------------------------------------------------------


def test_the_response_says_which_run_it_is_and_when_it_was_built(client):
    anchor = client.get(BASE).json()["anchor"]
    assert anchor["month"] == "2026-10" and anchor["run_id"] == 7
    assert anchor["generated_at"].startswith("2026-11-26T02:30")


def test_a_request_cannot_recalculate_the_month(client):
    # The month belongs to the stored run; an ``anchor`` query parameter is ignored, not obeyed.
    assert client.get(BASE, params={"anchor": "2026-09"}).json()["anchor"]["month"] == "2026-10"


def test_before_the_first_run_the_list_says_it_has_not_been_built(monkeypatch):
    from app.api.i7 import reverse_conversion as module
    from app.schemas.i7.errors import ApiError

    monkeypatch.setattr(module, "load_latest_report", lambda session: None)
    with pytest.raises(ApiError) as caught:
        module.get_reverse_report(session=None)
    assert caught.value.status_code == 409 and caught.value.code == "REVERSE_NOT_BUILT"


def test_an_item_carries_the_newest_conversion_request_when_there_is_one(client):
    BRIEFS[("REC1", "1300")] = (41, "PENDING_APPROVAL", "Inventory Controller")
    items = {i["material"]: i for i in client.get(BASE, params={"page_size": 100}).json()["items"]}
    assert items["REC1"]["conversion_request"] == {"id": 41, "status": "PENDING_APPROVAL", "pending_role": "Inventory Controller"}
    assert items["REC2"]["conversion_request"] is None


# --- The CSV export -----------------------------------------------------------------------------------------


def test_the_export_is_a_csv_attachment_with_the_anchor_in_its_name_and_headers(client):
    response = client.get(f"{BASE}/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="i7-min-max-to-oar-2026-10.csv"'
    assert response.headers["x-anchor-month"] == "2026-10" and response.headers["x-policy-version"] == "reverse-conversion-v2"
    assert response.content.startswith(b"\xef\xbb\xbf")


def test_the_export_holds_every_row_not_just_a_page(client):
    rows = list(csv.DictReader(io.StringIO(client.get(f"{BASE}/export").content.decode("utf-8-sig"), newline="")))
    assert len(rows) == 18
    assert {r["anchor_month"] for r in rows} == {"2026-10"}


def test_the_export_follows_the_same_filters_as_the_list(client):
    rows = list(csv.DictReader(io.StringIO(client.get(f"{BASE}/export", params={"reason": "obsolete_criticality"}).content.decode("utf-8-sig"), newline="")))
    assert {r["material"] for r in rows} == {"OBS"}


def test_the_export_refuses_a_bad_filter_like_the_list(client):
    assert client.get(f"{BASE}/export", params={"plant": "9999"}).status_code == 400


# --- Isolation from the existing recommendation routes ---------------------------------------------------------


def registered_paths() -> dict[str, set[str]]:
    """Every registered route as path -> HTTP methods, from the OpenAPI document (FastAPI wraps included
    routers, so ``app.routes`` does not list their paths)."""
    return {path: set(operations) for path, operations in app.openapi()["paths"].items()}


def test_the_new_routes_live_under_their_own_prefix():
    paths = set(registered_paths())
    assert "/api/v1/i7/reverse-conversion/recommendations" in paths
    assert "/api/v1/i7/reverse-conversion/recommendations/export" in paths
    new = {p for p in paths if "reverse-conversion" in p}
    assert all(not p.startswith("/api/v1/i7/recommendations") for p in new)


def test_the_existing_recommendation_routes_are_still_registered_unchanged():
    paths = set(registered_paths())
    for existing in (
        "/api/v1/i7/recommendations",
        "/api/v1/i7/recommendations/export",
        "/api/v1/i7/recommendations/summary",
        "/api/v1/i7/recommendations/{recommendation_id}",
    ):
        assert existing in paths


def test_the_recommendation_endpoints_are_read_only_and_only_requests_write():
    paths = registered_paths()
    reading = {method for path, found in paths.items() if "reverse-conversion/recommendations" in path for method in found}
    assert reading == {"get"}
    writing = {path for path, found in paths.items() if "reverse-conversion" in path and "post" in found}
    assert writing == {
        "/api/v1/i7/reverse-conversion/requests",
        "/api/v1/i7/reverse-conversion/requests/decline",
        "/api/v1/i7/reverse-conversion/requests/{request_id}/actions",
    }


# --- Confidence -------------------------------------------------------------------------------------


def test_rows_carry_a_confidence_only_in_convert_to_oar(client):
    items = client.get(BASE, params={"page_size": 100}).json()["items"]
    for item in items:
        if item["category"] == "recommended_for_oar":
            assert item["confidence"] in {"high", "medium", "low"} and item["confidence_label"] in {"High", "Medium", "Low"}
        else:
            assert item["confidence"] is None and item["confidence_label"] is None


def test_the_summary_counts_convert_to_oar_by_confidence(client):
    by_confidence = client.get(BASE).json()["summary"]["by_confidence"]
    assert [c["confidence"] for c in by_confidence] == ["high", "medium", "low"]
    assert sum(c["count"] for c in by_confidence) == 15


def test_the_two_zero_issue_reasons_are_shown_in_the_planners_words(client):
    items = {i["material"]: i for i in client.get(BASE, params={"page_size": 100}).json()["items"]}
    assert items["NEVER"]["category"] == "recommended_for_oar"
    assert items["NEVER"]["reason_label"] == "Zero issues in 24 months and never moved"
    assert items["ZERO24"]["reason_label"] == "Zero issues in 24 months but moved before"


def test_an_unknown_confidence_is_refused(client):
    assert client.get(BASE, params={"confidence": "certain"}).status_code == 422


def test_the_export_has_a_confidence_column_and_follows_the_confidence_filter(client):
    text = client.get(f"{BASE}/export", params={"confidence": "low"}).content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    assert {r["material"] for r in rows} == {"UNKCRIT", "INCONS", "LONGLT", "NOLT"}
    assert {r["confidence"] for r in rows} == {"Low"}


# --- One material-plant's consumption (the detail chart) -----------------------------------------------

CONSUMPTION = f"{BASE}/{{material}}/{{plant}}/consumption"


def test_the_consumption_series_is_24_zero_filled_months_ending_at_the_anchor(client):
    body = client.get(CONSUMPTION.format(material="REC1", plant="1300")).json()
    months = [m["month"] for m in body["months"]]
    assert len(months) == 24 and months[0] == "2024-11" and months[-1] == "2026-10"
    assert body["anchor_month"] == "2026-10" and body["latest_window_start"] == "2025-11"
    assert any(float(m["net_issued_quantity"]) > 0 for m in body["months"])
    assert any(float(m["net_issued_quantity"]) == 0 for m in body["months"])  # gaps are shown, not skipped


def test_a_never_moved_material_has_a_flat_series(client):
    body = client.get(CONSUMPTION.format(material="NEVER", plant="1300")).json()
    assert len(body["months"]) == 24 and all(float(m["net_issued_quantity"]) == 0 for m in body["months"])


def test_an_unlisted_material_plant_is_a_404_and_a_bad_plant_a_400(client):
    assert client.get(CONSUMPTION.format(material="NOPE", plant="1300")).status_code == 404
    assert client.get(CONSUMPTION.format(material="REC1", plant="9999")).status_code == 400


def test_the_consumption_endpoint_measures_to_the_stored_runs_month(client):
    body = client.get(CONSUMPTION.format(material="REC1", plant="1300")).json()
    assert body["anchor_month"] == "2026-10" and body["months"][-1]["month"] == "2026-10"


# --- Eligible / Not eligible (the OAR conversion column) -------------------------------------------------


def test_every_row_says_whether_it_could_move_to_oar(client):
    items = client.get(BASE, params={"page_size": 100}).json()["items"]
    for item in items:
        expected = "eligible" if item["category"] == "recommended_for_oar" else "not_eligible"
        assert item["conversion_eligibility"] == expected, item["material"]


def test_the_eligibility_filter_splits_the_population(client):
    eligible = materials(client.get(BASE, params={"eligibility": "eligible", "page_size": 100}))
    not_eligible = materials(client.get(BASE, params={"eligibility": "not_eligible", "page_size": 100}))
    assert len(eligible) == 15 and len(not_eligible) == 3
    assert not set(eligible) & set(not_eligible)
    assert {"PROT", "NOHIST", "FREQ"} == set(not_eligible)
    assert "OBS" in eligible


def test_not_eligible_rows_have_no_confidence_but_have_a_reason(client):
    for item in client.get(BASE, params={"eligibility": "not_eligible", "page_size": 100}).json()["items"]:
        assert item["confidence"] is None and item["reason_label"] and item["explanation"]


def test_an_unknown_eligibility_is_refused(client):
    assert client.get(BASE, params={"eligibility": "maybe"}).status_code == 422


def test_the_export_has_an_oar_conversion_column_and_follows_the_filter(client):
    text = client.get(f"{BASE}/export", params={"eligibility": "not_eligible"}).content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    assert {r["oar_conversion"] for r in rows} == {"Not eligible"} and len(rows) == 3
    everything = list(csv.DictReader(io.StringIO(client.get(f"{BASE}/export").content.decode("utf-8-sig"), newline="")))
    assert {r["oar_conversion"] for r in everything} == {"Eligible", "Not eligible"}


def test_not_eligible_rows_can_still_open_their_consumption(client):
    assert client.get(CONSUMPTION.format(material="FREQ", plant="1300")).status_code == 200


# --- The Min-Max alternative beside Medium and Low confidence --------------------------------------------


def _by_confidence(client):
    items = client.get(BASE, params={"category": "recommended_for_oar", "page_size": 100}).json()["items"]
    out: dict[str, list[str]] = {"high": [], "medium": [], "low": []}
    for item in items:
        out[item["confidence"]].append(item["material"])
    return out


def test_the_stock_item_recommendation_is_looked_up_only_for_medium_and_low_confidence_rows(client):
    groups = _by_confidence(client)
    client.get(BASE, params={"page_size": 100})
    assert {m for m, _ in ASKED} == set(groups["medium"] + groups["low"])  # exactly the Medium and Low rows of the page
    assert not set(m for m, _ in ASKED) & set(groups["high"])  # High stands alone: nothing is fetched for it


def test_a_medium_row_carries_the_alternative_and_a_high_row_does_not(client):
    groups = _by_confidence(client)
    medium, high = groups["medium"][0], groups["high"][0]
    ALTERNATIVES[(medium, "1300")] = MinMaxAlternative(Decimal(12), Decimal(8), Decimal(40), Decimal(24))
    ALTERNATIVES[(high, "1300")] = MinMaxAlternative(Decimal(5), Decimal(4), Decimal(9), Decimal(7))  # never shown
    items = {(i["material"], i["plant"]): i for i in client.get(BASE, params={"page_size": 100}).json()["items"]}
    assert items[(medium, "1300")]["min_max_alternative"] == {
        "current_rop": "12", "recommended_rop": "8", "current_max_stock": "40", "recommended_max_stock": "24",
    }
    assert items[(high, "1300")]["min_max_alternative"] is None


def test_a_low_row_carries_it_too(client):
    low = _by_confidence(client)["low"][0]
    ALTERNATIVES[(low, "1300")] = MinMaxAlternative(Decimal(6), Decimal(4), Decimal(18), Decimal(12))
    items = {(i["material"], i["plant"]): i for i in client.get(BASE, params={"page_size": 100}).json()["items"]}
    assert items[(low, "1300")]["min_max_alternative"]["recommended_rop"] == "4"


def test_a_row_with_no_stock_item_recommendation_simply_has_no_alternative(client):
    medium = _by_confidence(client)["medium"][0]
    items = {i["material"]: i for i in client.get(BASE, params={"page_size": 100}).json()["items"]}
    assert items[medium]["min_max_alternative"] is None


def test_a_not_eligible_row_has_no_alternative(client):
    for item in client.get(BASE, params={"eligibility": "not_eligible", "page_size": 100}).json()["items"]:
        assert item["min_max_alternative"] is None


def test_every_item_carries_its_reason_as_three_labelled_lines(client):
    for item in client.get(BASE, params={"page_size": 100}).json()["items"]:
        assert [p["label"] for p in item["explanation_points"]] == ["What we saw", "What it means", "What to do"], item["material"]
        assert item["explanation"]  # the one-paragraph form is still there (the CSV uses it)
