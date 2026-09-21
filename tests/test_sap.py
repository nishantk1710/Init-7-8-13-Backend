"""SAP client tests.

Almost all of these use a fake transport, so the suite needs no network and no
credentials. The handful that talk to live CPI skip unless it is configured --
they are marked, and their skip reason says so, because a silent skip looks
exactly like a pass.

The cases worth reading are the ones encoding something we measured rather than
assumed:

* ``test_unordered_paging_is_impossible`` -- the 25%-row-loss finding.
* ``TestFilterGuard`` -- the 62 silently ignored filters.
* ``test_decimal_arriving_as_string_still_decodes`` -- the Edm.Decimal drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from app.core.config import Settings, get_settings
from app.integrations.sap.client import SapClient, _query
from app.integrations.sap.contract import contract, entity_set
from app.integrations.sap.envelope import decode_count, decode_rows, decode_value
from app.integrations.sap.errors import (
    AuthError,
    ContractError,
    NotFoundError,
    RequestError,
    SapError,
    TransientError,
    UnsupportedFilterError,
    classify,
)
from app.integrations.sap.filters import (
    IGNORED,
    check_filter,
    properties_in,
    unsupported_properties,
    verdict_for,
)
from app.integrations.sap.paging import SAFETY_ROW_LIMIT
from app.integrations.sap.token import TokenProvider
from app.integrations.sap.transport import CpiTransport


# --- Test doubles ---------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    _json: Any = None

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def content(self) -> bytes:
        return self.text.encode()

    def json(self) -> Any:
        if self._json is not None:
            return self._json
        return json.loads(self.text)


def settings(**overrides) -> Settings:
    base = {
        "cpi_base_url": "https://cpi.example.test",
        "cpi_token_url": "https://cpi.example.test/oauth/token",
        "cpi_client_id": "id",
        "cpi_client_secret": "secret",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def token_provider(*responses: FakeResponse) -> TokenProvider:
    queue = list(responses) or [FakeResponse(text='{"access_token":"t","expires_in":3600}')]

    def transport(url, *, data, auth, timeout, verify=True):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return TokenProvider(settings(), transport=transport)


def transport_returning(*responses: FakeResponse, capture: list | None = None) -> CpiTransport:
    queue = list(responses)

    def http(url, *, params, headers, timeout, verify=True):
        if capture is not None:
            capture.append(params)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return CpiTransport(
        settings(),
        tokens=token_provider(),
        transport=http,
        sleep=lambda _seconds: None,  # never actually wait in a test
    )


def feed(rows: list[dict]) -> str:
    return json.dumps({"d": {"results": rows}})


def routed_transport(
    *, count: FakeResponse | None = None, pages: list[FakeResponse], capture: list | None = None
) -> CpiTransport:
    """A fake that answers $count and row reads separately.

    read_all() makes both kinds of call, so a single response queue makes the
    $count consume a row page (or vice versa) and the test fails for a reason
    that has nothing to do with the code under test.
    """
    remaining = list(pages)

    def http(url, *, params, headers, timeout, verify=True):
        if capture is not None:
            capture.append(params)
        if params["APIPath"].endswith("/$count"):
            return count or FakeResponse(status_code=500)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return CpiTransport(
        settings(),
        tokens=token_provider(),
        transport=http,
        sleep=lambda _seconds: None,
    )


# --- Error taxonomy -------------------------------------------------------


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, AuthError),
            (403, AuthError),
            (404, NotFoundError),
            (400, RequestError),
            (429, RequestError),
            (500, TransientError),
            (503, TransientError),
        ],
    )
    def test_status_maps_to_the_right_class(self, status: int, expected: type) -> None:
        assert isinstance(classify(status, "", "ctx"), expected)

    def test_message_names_what_failed(self) -> None:
        assert "MaterialPlantSet" in str(classify(404, "", "MaterialPlantSet"))

    def test_body_is_truncated_so_logs_survive_an_html_error_page(self) -> None:
        error = SapError("x", status=500, body="y" * 10_000)
        assert error.body is not None and len(error.body) == 2000


# --- Token ----------------------------------------------------------------


class TestToken:
    def test_fetches_and_caches(self) -> None:
        calls = []

        def transport(url, *, data, auth, timeout, verify=True):
            calls.append(url)
            return FakeResponse(text='{"access_token":"abc","expires_in":3600}')

        provider = TokenProvider(settings(), transport=transport)
        assert provider.get() == "abc"
        assert provider.get() == "abc"
        assert len(calls) == 1, "second get() should use the cached token"

    def test_invalidate_forces_a_refetch(self) -> None:
        calls = []

        def transport(url, *, data, auth, timeout, verify=True):
            calls.append(url)
            return FakeResponse(text='{"access_token":"abc","expires_in":3600}')

        provider = TokenProvider(settings(), transport=transport)
        provider.get()
        provider.invalidate()
        provider.get()
        assert len(calls) == 2

    def test_missing_settings_raise_auth_error_not_sys_exit(self) -> None:
        """The script called sys.exit here. A web worker must not."""
        provider = TokenProvider(settings(cpi_client_secret=""))
        with pytest.raises(AuthError, match="CPI_CLIENT_SECRET"):
            provider.get()

    def test_rejected_credentials_raise_auth_error(self) -> None:
        provider = TokenProvider(
            settings(), transport=lambda *a, **k: FakeResponse(status_code=401, text="nope")
        )
        with pytest.raises(AuthError):
            provider.get()

    def test_response_without_a_token_is_an_auth_error(self) -> None:
        provider = TokenProvider(
            settings(), transport=lambda *a, **k: FakeResponse(text='{"expires_in":3600}')
        )
        with pytest.raises(AuthError, match="access_token"):
            provider.get()


# --- Transport ------------------------------------------------------------


class TestTransport:
    def test_sends_apipath_and_apiquery(self) -> None:
        captured: list[dict] = []
        transport = transport_returning(FakeResponse(text="ok"), capture=captured)
        transport.get("sap/opu/odata/sap/SRV/Set", "$top=1")
        assert captured[0] == {"APIPath": "sap/opu/odata/sap/SRV/Set", "APIQuery": "$top=1"}

    def test_401_refreshes_the_token_and_retries_once(self) -> None:
        transport = transport_returning(
            FakeResponse(status_code=401), FakeResponse(text="second time lucky")
        )
        assert transport.get("path") == "second time lucky"

    def test_a_second_401_is_a_real_auth_failure(self) -> None:
        transport = transport_returning(
            FakeResponse(status_code=401), FakeResponse(status_code=401)
        )
        with pytest.raises(AuthError):
            transport.get("path")

    def test_5xx_retries_then_raises_transient(self) -> None:
        transport = transport_returning(FakeResponse(status_code=500))
        with pytest.raises(TransientError):
            transport.get("path")

    def test_5xx_that_recovers_is_not_an_error(self) -> None:
        transport = transport_returning(FakeResponse(status_code=503), FakeResponse(text="ok"))
        assert transport.get("path") == "ok"

    def test_404_is_never_retried(self) -> None:
        calls: list[dict] = []
        transport = transport_returning(FakeResponse(status_code=404), capture=calls)
        with pytest.raises(NotFoundError):
            transport.get("path")
        assert len(calls) == 1, "a 404 must not be retried -- the URL is wrong"

    def test_accept_header_tolerates_xml(self) -> None:
        """$metadata is XML only; asking for JSON alone earns an HTTP 406."""
        seen: list[dict] = []

        def http(url, *, params, headers, timeout, verify=True):
            seen.append(headers)
            return FakeResponse(text="<edmx/>")

        CpiTransport(
            settings(), tokens=token_provider(), transport=http, sleep=lambda _s: None
        ).get("sap/opu/odata/sap/SRV/$metadata")

        accept = seen[0]["Accept"]
        assert "application/json" in accept
        assert "application/xml" in accept

    def test_bearer_token_is_sent(self) -> None:
        seen: list[dict] = []

        def http(url, *, params, headers, timeout, verify=True):
            seen.append(headers)
            return FakeResponse(text="ok")

        CpiTransport(
            settings(), tokens=token_provider(), transport=http, sleep=lambda _s: None
        ).get("path")
        assert seen[0]["Authorization"].startswith("Bearer ")

    def test_missing_base_url_is_reported_clearly(self) -> None:
        transport = CpiTransport(settings(cpi_base_url=""), tokens=token_provider())
        with pytest.raises(SapError, match="CPI_BASE_URL"):
            transport.get("path")


# --- Envelope and decoding ------------------------------------------------


class TestDecoding:
    def test_unwraps_the_double_envelope(self) -> None:
        result = decode_rows(feed([{"Matnr": "1", "Werks": "1300"}]), entity_set("MaterialPlantSet"))
        assert result.rows == [{"Matnr": "1", "Werks": "1300"}]

    def test_unwraps_a_single_entity(self) -> None:
        body = json.dumps({"d": {"Matnr": "1", "Werks": "1300"}})
        assert len(decode_rows(body, entity_set("MaterialPlantSet")).rows) == 1

    def test_metadata_key_is_dropped(self) -> None:
        body = feed([{"Matnr": "1", "__metadata": {"uri": "..."}}])
        assert "__metadata" not in decode_rows(body, entity_set("MaterialPlantSet")).rows[0]

    def test_material_numbers_keep_their_padding(self) -> None:
        body = feed([{"Matnr": "000000000080000000"}])
        rows = decode_rows(body, entity_set("MaterialPlantSet")).rows
        assert rows[0]["Matnr"] == "000000000080000000"

    def test_decimal_arriving_as_string_still_decodes(self) -> None:
        """Netpr/Netwr changed Edm.Decimal -> Edm.String between two sweeps."""
        assert decode_value("12.34", "Edm.Decimal") == Decimal("12.34")
        assert decode_value(12.34, "Edm.Decimal") == Decimal("12.34")

    def test_decimals_are_exact_not_floats(self) -> None:
        assert decode_value("0.1", "Edm.Decimal") + decode_value("0.2", "Edm.Decimal") == Decimal(
            "0.3"
        )

    def test_sap_dates_decode(self) -> None:
        decoded = decode_value("/Date(1379030400000)/", "Edm.DateTime")
        assert decoded.year == 2013

    def test_sap_time_decodes(self) -> None:
        assert decode_value("PT14H30M00S", "Edm.Time") == "14:30:00"

    def test_sap_flag_booleans(self) -> None:
        assert decode_value("X", "Edm.Boolean") is True
        assert decode_value("", "Edm.Boolean") is False

    def test_none_stays_none(self) -> None:
        assert decode_value(None, "Edm.Decimal") is None

    def test_unknown_property_is_kept_and_reported(self) -> None:
        """Dropping data would be worse; but new fields are drift worth knowing."""
        body = feed([{"Matnr": "1", "BrandNewField": "x"}])
        result = decode_rows(body, entity_set("MaterialPlantSet"))
        assert result.rows[0]["BrandNewField"] == "x"
        assert result.unknown_properties == ["BrandNewField"]

    def test_non_json_body_is_a_contract_error(self) -> None:
        with pytest.raises(ContractError, match="not JSON"):
            decode_rows("<html>gateway timeout</html>", entity_set("MaterialPlantSet"))

    def test_missing_envelope_is_a_contract_error(self) -> None:
        with pytest.raises(ContractError, match="envelope"):
            decode_rows(json.dumps({"results": []}), entity_set("MaterialPlantSet"))

    def test_count_parses(self) -> None:
        assert decode_count("2178", "x") == 2178
        assert decode_count('"2178"', "x") == 2178

    def test_count_of_an_html_error_is_a_contract_error(self) -> None:
        with pytest.raises(ContractError):
            decode_count("<html>500</html>", "x")


# --- Contract -------------------------------------------------------------


class TestContract:
    def test_all_21_sets_across_two_services(self) -> None:
        sets = contract()
        assert len(sets) == 21
        assert {s.service for s in sets.values()} == {
            "ZVZI_KPI02_SHARED_SRV",
            "ZMM_KPI02_SRV",
        }

    def test_every_set_has_a_key_and_properties(self) -> None:
        for name, spec in contract().items():
            assert spec.keys, f"{name} has no key -- it could not be paged safely"
            assert spec.properties, f"{name} has no properties"

    def test_every_key_is_a_real_property(self) -> None:
        for name, spec in contract().items():
            for key in spec.keys:
                assert spec.find(key), f"{name}: key {key} is not a property"

    def test_key_order_follows_sap_not_the_alphabet(self) -> None:
        """An OData key predicate is positional, so order is not cosmetic."""
        assert entity_set("StorageLocationStockSet").keys == ("Matnr", "Werks", "Lgort")

    def test_purchase_requisition_key_includes_bnfpo(self) -> None:
        """Phase 12 recorded SAP adding Bnfpo. A regression here means a stale snapshot."""
        assert entity_set("PurchaseRequisitionSet").keys == ("Banfn", "Bnfpo")

    def test_api_path_is_built_from_the_owning_service(self) -> None:
        assert (
            entity_set("MaterialPlantSet").api_path
            == "sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/MaterialPlantSet"
        )

    def test_unknown_set_lists_the_alternatives(self) -> None:
        with pytest.raises(ContractError, match="Exposed sets"):
            entity_set("NoSuchSet")


# --- The filter guard -----------------------------------------------------


class TestFilterGuard:
    @pytest.mark.drift
    def test_pstyp_is_known_to_be_ignored(self) -> None:
        """The exact case behind 'apply the Pstyp filter client-side'."""
        assert verdict_for("PurchaseOrderItemSet", "Pstyp") == IGNORED

    @pytest.mark.drift
    def test_filtering_on_an_ignored_property_is_refused(self) -> None:
        with pytest.raises(UnsupportedFilterError, match="SILENTLY IGNORES"):
            check_filter("PurchaseOrderItemSet", "Pstyp eq '3'")

    def test_an_honoured_filter_passes(self) -> None:
        check_filter("MaterialPlantSet", "Dismm eq 'ND'")  # must not raise

    def test_no_filter_passes(self) -> None:
        check_filter("MaterialPlantSet", None)

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("Dismm eq 'ND'", ["Dismm"]),
            ("Dismm eq 'ND' and Werks eq '1300'", ["Dismm", "Werks"]),
            ("startswith(Matnr,'80')", ["Matnr"]),
            ("Matnr eq 'Dismm'", ["Matnr"]),  # a literal must not read as a field
        ],
    )
    def test_extracts_property_names(self, expression: str, expected: list[str]) -> None:
        assert properties_in(expression) == expected

    @pytest.mark.drift
    def test_override_is_available_for_a_re_verified_filter(self) -> None:
        assert unsupported_properties("PurchaseOrderItemSet", "Pstyp eq '3'") == {
            "Pstyp": IGNORED
        }


# --- Query building -------------------------------------------------------


class TestQueryBuilding:
    def test_orders_and_pages(self) -> None:
        assert _query(order_by=["Matnr", "Werks"], top=10, skip=20) == (
            "$orderby=Matnr,Werks&$top=10&$skip=20&$format=json"
        )

    def test_count_queries_omit_json_format(self) -> None:
        assert _query(filter="Dismm eq 'ND'", json_format=False) == "$filter=Dismm eq 'ND'"


# --- Client behaviour -----------------------------------------------------


class TestClient:
    def test_read_decodes_rows(self) -> None:
        client = SapClient(
            settings(), transport_returning(FakeResponse(text=feed([{"Matnr": "1"}])))
        )
        assert client.read("MaterialPlantSet").rows == [{"Matnr": "1"}]

    def test_empty_is_not_a_failure(self) -> None:
        """Two live sets legitimately return zero rows."""
        client = SapClient(settings(), transport_returning(FakeResponse(text=feed([]))))
        result = client.read("MaterialValuationSet")
        assert result.is_empty and len(result) == 0

    def test_read_refuses_an_ignored_filter_before_calling(self) -> None:
        calls: list[dict] = []
        client = SapClient(
            settings(), transport_returning(FakeResponse(text=feed([])), capture=calls)
        )
        with pytest.raises(UnsupportedFilterError):
            client.read("PurchaseOrderItemSet", filter="Pstyp eq '3'")
        assert calls == [], "the guard must fire before any HTTP call"

    def test_count_returns_none_when_sap_500s(self) -> None:
        """Several sets serve rows fine but cannot count them."""
        client = SapClient(settings(), transport_returning(FakeResponse(status_code=500)))
        assert client.count("GoodsMovementItemSet") is None

    def test_a_capped_count_is_never_asked_for(self) -> None:
        """ReservationItemSet answers 1000 where paging returns 7088.

        A wrong-but-successful total is worse than a failed one. Paging stops
        once it has read as many rows as the total claims, so believing 1000
        hands the caller a seventh of the set and calls it complete -- every
        reservation-dependent figure computed on 14% of the data, silently.
        Measured 2026-09-21; see known_conditions.COUNT_CAPPED_SETS.
        """
        calls: list[dict] = []
        client = SapClient(
            settings(),
            transport_returning(FakeResponse(text="1000"), capture=calls),
        )

        assert client.count("ReservationItemSet") is None
        assert calls == [], "SAP must not even be asked for this count"

    def test_a_capped_count_does_not_stop_paging_early(self) -> None:
        """The whole point: with no count, paging runs to a short page."""
        page = feed([{"Rsnum": str(i), "Rspos": "1"} for i in range(2)])
        client = SapClient(
            settings(),
            routed_transport(
                count=FakeResponse(text="1"),
                pages=[
                    FakeResponse(text=page),
                    FakeResponse(text=feed([{"Rsnum": "99", "Rspos": "1"}])),
                ],
            ),
        )

        result = client.read_all("ReservationItemSet", page_size=2)

        # A trusted count of 1 would have stopped after the first page.
        assert len(result) == 3
        assert result.counted is False

    def test_read_all_always_orders_by_the_key(self) -> None:
        """The 25%-row-loss finding, encoded as a test."""
        calls: list[dict] = []
        client = SapClient(
            settings(),
            routed_transport(
                count=FakeResponse(text="1"),
                pages=[FakeResponse(text=feed([{"Matnr": "1"}]))],
                capture=calls,
            ),
        )
        client.read_all("MaterialPlantSet", page_size=10)
        row_calls = [c for c in calls if "$count" not in c["APIPath"]]
        assert row_calls, "expected at least one row request"
        for call in row_calls:
            assert "$orderby=Matnr,Werks" in call["APIQuery"]

    def test_read_all_pages_until_a_short_page(self) -> None:
        full = feed([{"Matnr": str(i)} for i in range(2)])
        short = feed([{"Matnr": "final"}])
        client = SapClient(
            settings(),
            routed_transport(
                count=FakeResponse(status_code=500),  # $count fails on this set
                pages=[
                    FakeResponse(text=full),
                    FakeResponse(text=full),
                    FakeResponse(text=short),
                ],
            ),
        )
        result = client.read_all("MaterialPlantSet", page_size=2)
        assert len(result) == 5
        assert result.pages == 3
        assert result.counted is False, "should have fallen back"
        assert result.complete

    def test_read_all_stops_at_count_when_it_works(self) -> None:
        page = feed([{"Matnr": "a"}, {"Matnr": "b"}])
        client = SapClient(
            settings(),
            routed_transport(count=FakeResponse(text="2"), pages=[FakeResponse(text=page)]),
        )
        result = client.read_all("MaterialPlantSet", page_size=2)
        assert result.counted and result.expected == 2 and result.complete

    def test_large_sets_refuse_an_unfiltered_read(self) -> None:
        client = SapClient(settings(), transport_returning(FakeResponse(text=feed([]))))
        with pytest.raises(SapError, match="too large"):
            client.read_all("ChangeDocItemSet")

    def test_large_sets_allow_a_filtered_read(self) -> None:
        client = SapClient(
            settings(),
            routed_transport(
                count=FakeResponse(text="1"),
                pages=[FakeResponse(text=feed([{"Changenr": "1"}]))],
            ),
        )
        result = client.read_all("ChangeDocItemSet", filter="Objectclas eq 'MATERIAL'")
        assert len(result) == 1

    def test_falls_back_when_sap_rejects_the_full_key_ordering(self) -> None:
        """Reproduces the live 2026-09-11 defect: MaterialPlantSet 500s on any
        two-field $orderby, while single-field works."""
        from app.integrations.sap.paging import extract, Page
        from app.integrations.sap.contract import entity_set as get_set

        attempted: list[list[str]] = []

        def read_page(*, skip: int, top: int, order_by: list[str]) -> Page:
            attempted.append(list(order_by))
            if len(order_by) > 1:
                raise TransientError("MaterialPlantSet: SAP returned a server error", status=500)
            return Page(rows=[{"Matnr": "1", "Werks": "1300"}])

        result = extract(get_set("MaterialPlantSet"), read_page, page_size=10, count=1)

        assert attempted[0] == ["Matnr", "Werks"], "the full key must be tried first"
        assert result.order_by == ("Matnr",), "should fall back to the longest prefix"
        assert result.order_by_degraded is True
        assert len(result) == 1

    def test_full_key_ordering_costs_no_extra_request(self) -> None:
        """The common case must not pay for the fallback."""
        from app.integrations.sap.paging import extract, Page
        from app.integrations.sap.contract import entity_set as get_set

        calls: list[int] = []

        def read_page(*, skip: int, top: int, order_by: list[str]) -> Page:
            calls.append(skip)
            return Page(rows=[{"Matnr": "1", "Werks": "1300"}])

        extract(get_set("MaterialPlantSet"), read_page, page_size=10, count=1)
        assert calls == [0], "one page of data should mean exactly one request"

    def test_refuses_when_no_ordering_is_accepted(self) -> None:
        """Reading unordered would silently lose rows, so refuse instead."""
        from app.integrations.sap.paging import extract, Page
        from app.integrations.sap.contract import entity_set as get_set

        def read_page(*, skip: int, top: int, order_by: list[str]) -> Page:
            raise TransientError("nope", status=500)

        with pytest.raises(SapError, match=r"rejected every \$orderby"):
            extract(get_set("MaterialPlantSet"), read_page, page_size=10, count=1)

    def test_duplicate_keys_are_counted_and_reported_as_unstable(self) -> None:
        """The direct check: overlapping pages mean rows are missing."""
        from app.integrations.sap.paging import extract, Page
        from app.integrations.sap.contract import entity_set as get_set

        pages = [
            Page(rows=[{"Matnr": "1", "Werks": "A"}, {"Matnr": "2", "Werks": "B"}]),
            Page(rows=[{"Matnr": "2", "Werks": "B"}]),  # overlap: row 2 again
        ]

        def read_page(*, skip: int, top: int, order_by: list[str]) -> Page:
            return pages.pop(0) if pages else Page(rows=[])

        result = extract(get_set("MaterialPlantSet"), read_page, page_size=2, count=3)
        assert result.duplicate_keys == 1
        assert result.stable is False

    def test_a_clean_extraction_is_stable(self) -> None:
        from app.integrations.sap.paging import extract, Page
        from app.integrations.sap.contract import entity_set as get_set

        def read_page(*, skip: int, top: int, order_by: list[str]) -> Page:
            return Page(rows=[{"Matnr": str(skip), "Werks": "A"}]) if skip < 2 else Page(rows=[])

        result = extract(get_set("MaterialPlantSet"), read_page, page_size=1, count=2)
        assert result.stable and result.duplicate_keys == 0

    def test_safety_limit_is_above_the_largest_known_set(self) -> None:
        """ChangeDocItemSet is about 929,000 rows; the cap must not clip it."""
        assert SAFETY_ROW_LIMIT > 1_000_000


# --- Live CPI (skipped unless configured) ---------------------------------

# Two gates, deliberately. The marker keeps these out of the default run and CI
# (see pytest.ini); the skipif means that even `pytest -m live` says plainly
# "not configured" rather than failing with an auth error on a machine that was
# never going to reach SAP.
live = pytest.mark.live
needs_cpi = pytest.mark.skipif(
    not get_settings().cpi_configured,
    reason="CPI not configured (CPI_BASE_URL / CPI_TOKEN_URL / CPI_CLIENT_ID / CPI_CLIENT_SECRET)",
)


@live
@needs_cpi
class TestAgainstLiveCpi:
    def test_reads_a_page(self) -> None:
        result = SapClient().read("MaterialPlantSet", top=5)
        assert len(result) == 5
        assert result.unknown_properties == [], "SAP returned a property we do not know"

    def test_count_is_close_to_the_snapshot(self) -> None:
        """Tolerates growth, fails on a step change.

        Was an exact-equality assertion, which failed on 2026-09-11 for
        2183 vs 2178 -- five new rows in a live system. That is the snapshot
        ageing normally, not a defect, and a test that cannot tell the two apart
        trains people to ignore it. A 5% swing still fails.
        """
        from app.integrations.sap.contract import counts

        live_count = SapClient().count("MaterialPlantSet")
        assert live_count is not None, "$count stopped working on MaterialPlantSet"
        recorded = int(counts()["MaterialPlantSet"])
        assert abs(live_count - recorded) <= max(100, recorded * 0.05), (
            f"MaterialPlantSet moved {recorded} -> {live_count}; re-run cpi_discovery.py"
        )

    def test_full_read_loses_no_rows(self) -> None:
        """Every row read exactly once -- measured, not inferred from the ordering.

        Previously asserted distinct keys directly. That was right but
        incomplete: on 2026-09-11 this did not return duplicates, it returned
        nothing at all, because SAP 500s on any two-field $orderby for this set.
        The client now falls back to a shorter ordering, so the test checks both
        that the read SUCCEEDS and that it did not lose rows -- and reports the
        degradation rather than hiding it.
        """
        result = SapClient().read_all("MaterialPlantSet")

        assert len(result) > 0, "read_all returned nothing"
        assert result.stable, (
            f"{result.duplicate_keys} duplicate key(s) in {len(result)} rows -- "
            "paging lost rows"
        )
        if result.order_by_degraded:
            pytest.fail(
                f"Read succeeded but SAP refused the full key ordering; used "
                f"{result.order_by}. Rows were not lost this time, but that "
                "ordering is not unique so stability is not guaranteed. Raise the "
                "$orderby HTTP 500 with SAP Basis."
            )
