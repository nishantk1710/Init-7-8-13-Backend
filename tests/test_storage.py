"""Storage conformance suite.

Everything in ``TestStorageConformance`` is written against the ``Storage``
interface, never against a concrete adapter. Azure Data Lake is now the only
implementation, and it is exercised here through an in-memory fake of the Azure
SDK (``tests/fake_adls.py``) rather than against the real account.

That distinction matters. These tests prove the adapter's *behaviour* -- that a
failed write leaves no object, that listings are recursive and sorted, that a
missing key raises the right error. They do not prove RBAC, private endpoints,
DNS or throughput, and nothing here should be cited as evidence that they work.
The first deployment is what proves those.

Tests outside that class cover the factory, key validation and the readiness
endpoint, which are adapter-independent.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.storage import (
    InvalidKeyError,
    ObjectNotFoundError,
    Storage,
    StorageError,
    StorageNotConfiguredError,
    build_storage,
    get_storage,
    reset_storage_cache,
    sha256_of,
    validate_key,
)
from app.integrations.storage.adls import AzureDataLakeStorage, parse_abfss
from app.main import app
from tests.fake_adls import FakeDataLakeServiceClient

client = TestClient(app)

# The URL shape the deployed app will actually carry, so the tests exercise a
# base prefix rather than the simpler container-root case.
FAKE_URL = "abfss://raw@stvziaicomnonprod.dfs.core.windows.net/extracts"


def fake_storage(url: str = FAKE_URL) -> AzureDataLakeStorage:
    """An empty Data Lake adapter backed by the in-memory fake."""
    return AzureDataLakeStorage(url, service_client=FakeDataLakeServiceClient())


@pytest.fixture(params=["adls"])
def storage(request: pytest.FixtureRequest) -> Storage:
    """One configured, empty ``Storage`` per implementation under test.

    Parameterised even with a single implementation: a second backing store is
    one entry here and the whole class below covers it, which is the property
    that made swapping the local folder out for Azure cheap.
    """
    if request.param == "adls":
        return fake_storage()
    raise AssertionError(f"Unknown storage implementation: {request.param}")


class TestStorageConformance:
    """Behaviour every adapter must share."""

    def test_write_then_read_round_trips(self, storage: Storage) -> None:
        with storage.open_write("a/b/thing.bin") as handle:
            handle.write(b"payload")
        with storage.open_read("a/b/thing.bin") as handle:
            assert handle.read() == b"payload"

    def test_write_creates_intermediate_prefixes(self, storage: Storage) -> None:
        """A key with prefixes must not require the caller to create them."""
        with storage.open_write("deep/nested/prefix/file.txt") as handle:
            handle.write(b"x")
        assert storage.exists("deep/nested/prefix/file.txt")

    def test_exists_before_and_after(self, storage: Storage) -> None:
        assert storage.exists("later.txt") is False
        with storage.open_write("later.txt") as handle:
            handle.write(b"x")
        assert storage.exists("later.txt") is True

    def test_write_replaces_existing_content(self, storage: Storage) -> None:
        with storage.open_write("k.txt") as handle:
            handle.write(b"first-and-longer")
        with storage.open_write("k.txt") as handle:
            handle.write(b"second")
        with storage.open_read("k.txt") as handle:
            assert handle.read() == b"second"

    def test_failed_write_leaves_no_object(self, storage: Storage) -> None:
        """An interrupted write must not leave a truncated object behind."""
        with pytest.raises(RuntimeError, match="boom"):
            with storage.open_write("partial.bin") as handle:
                handle.write(b"half")
                raise RuntimeError("boom")
        assert storage.exists("partial.bin") is False

    def test_failed_overwrite_leaves_the_original(self, storage: Storage) -> None:
        """The dangerous case: a failed rewrite must not destroy good content."""
        with storage.open_write("keep.bin") as handle:
            handle.write(b"original")
        with pytest.raises(RuntimeError):
            with storage.open_write("keep.bin") as handle:
                handle.write(b"clobbered")
                raise RuntimeError("boom")
        with storage.open_read("keep.bin") as handle:
            assert handle.read() == b"original"

    # --- append -----------------------------------------------------------
    #
    # SAP delivers a table as chunks of 50,000 records, each its own request,
    # assembled into one file. That is what append is for, so it is held to the
    # same bar as the rest of the port.

    def test_append_creates_the_object_when_absent(self, storage: Storage) -> None:
        size = storage.append("chunked.csv", b"HEADER\n")

        assert size == 7
        with storage.open_read("chunked.csv") as handle:
            assert handle.read() == b"HEADER\n"

    def test_append_concatenates_in_order(self, storage: Storage) -> None:
        storage.append("chunked.csv", b"a\n")
        storage.append("chunked.csv", b"b\n")
        storage.append("chunked.csv", b"c\n")

        with storage.open_read("chunked.csv") as handle:
            assert handle.read() == b"a\nb\nc\n"

    def test_append_extends_an_object_written_normally(self, storage: Storage) -> None:
        """The real sequence: chunk one is a write, every later chunk appends."""
        with storage.open_write("chunked.csv") as handle:
            handle.write(b"HEADER\nrow1\n")
        storage.append("chunked.csv", b"row2\n")

        with storage.open_read("chunked.csv") as handle:
            assert handle.read() == b"HEADER\nrow1\nrow2\n"

    def test_append_returns_the_new_total_size(self, storage: Storage) -> None:
        storage.append("chunked.csv", b"12345")

        assert storage.append("chunked.csv", b"678") == 8
        assert storage.stat("chunked.csv").size == 8

    def test_appending_nothing_changes_nothing(self, storage: Storage) -> None:
        storage.append("chunked.csv", b"kept")

        assert storage.append("chunked.csv", b"") == 4
        with storage.open_read("chunked.csv") as handle:
            assert handle.read() == b"kept"

    def test_append_refuses_a_traversal_key(self, storage: Storage) -> None:
        with pytest.raises(StorageError):
            storage.append("../escaped.csv", b"x")

    def test_read_missing_key_raises_object_not_found(self, storage: Storage) -> None:
        with pytest.raises(ObjectNotFoundError):
            with storage.open_read("nope.txt"):
                pass

    def test_stat_reports_size(self, storage: Storage) -> None:
        with storage.open_write("sized.bin") as handle:
            handle.write(b"0123456789")
        assert storage.stat("sized.bin").size == 10

    def test_stat_missing_key_raises_object_not_found(self, storage: Storage) -> None:
        with pytest.raises(ObjectNotFoundError):
            storage.stat("nope.txt")

    def test_list_is_recursive_and_sorted(self, storage: Storage) -> None:
        for key in ("b/2.txt", "a/1.txt", "top.txt"):
            with storage.open_write(key) as handle:
                handle.write(b"x")
        assert list(storage.list()) == ["a/1.txt", "b/2.txt", "top.txt"]

    def test_list_filters_by_prefix(self, storage: Storage) -> None:
        for key in ("extracts/one.txt", "extracts/two.txt", "other/three.txt"):
            with storage.open_write(key) as handle:
                handle.write(b"x")
        assert list(storage.list("extracts")) == ["extracts/one.txt", "extracts/two.txt"]

    def test_list_of_empty_storage_is_empty(self, storage: Storage) -> None:
        assert list(storage.list()) == []

    def test_list_of_unknown_prefix_is_empty(self, storage: Storage) -> None:
        assert list(storage.list("no/such/prefix")) == []

    def test_delete_removes_the_object(self, storage: Storage) -> None:
        with storage.open_write("bye.txt") as handle:
            handle.write(b"x")
        storage.delete("bye.txt")
        assert storage.exists("bye.txt") is False

    def test_delete_missing_key_raises_object_not_found(self, storage: Storage) -> None:
        with pytest.raises(ObjectNotFoundError):
            storage.delete("nope.txt")

    def test_reads_are_streamed_not_materialised(self, storage: Storage) -> None:
        """Partial reads must be possible -- the 143 MB extract depends on it."""
        with storage.open_write("big.bin") as handle:
            handle.write(b"abcdefghij" * 1000)
        with storage.open_read("big.bin") as handle:
            assert handle.read(10) == b"abcdefghij"
            assert handle.read(10) == b"abcdefghij"

    def test_traversal_key_is_refused(self, storage: Storage) -> None:
        with pytest.raises(StorageError):
            storage.exists("../escaped.txt")

    def test_backslash_key_is_refused(self, storage: Storage) -> None:
        with pytest.raises(StorageError):
            storage.exists("extracts\\MSEG_1.XLSX")

    def test_check_connection_passes_when_configured(self, storage: Storage) -> None:
        storage.check_connection()

    def test_sha256_matches_hashlib(self, storage: Storage) -> None:
        import hashlib

        payload = b"the quick brown fox" * 100
        with storage.open_write("hashed.bin") as handle:
            handle.write(payload)
        assert sha256_of(storage, "hashed.bin") == hashlib.sha256(payload).hexdigest()


class TestKeyValidation:
    @pytest.mark.parametrize("key", ["a.txt", "a/b.txt", "a/b/c-1_2.XLSX", "extracts/MSEG_1.XLSX"])
    def test_accepts_legal_keys(self, key: str) -> None:
        assert validate_key(key) == key

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "/absolute.txt",
            "../escape.txt",
            "a/../../escape.txt",
            "a//b.txt",
            "./relative.txt",
            "windows\\path.txt",
        ],
    )
    def test_rejects_illegal_keys(self, key: str) -> None:
        with pytest.raises(InvalidKeyError):
            validate_key(key)


class TestFactory:
    @pytest.mark.parametrize(
        "url",
        [
            "C:/Users/someone/OneDrive/Vedanta",
            "/mnt/data/extracts",
            "./extracts",
            "file:///D:/vzi-data",
        ],
    )
    def test_a_local_path_is_refused_and_explained(self, url: str) -> None:
        """A leftover local STORAGE_URL is the likeliest upgrade failure.

        urlparse reads "C:/data" as scheme "c", so without this the message
        would be "Unsupported STORAGE_URL scheme 'c'" -- true, and useless.
        """
        with pytest.raises(StorageError, match="Azure Data Lake only"):
            build_storage(url)

    def test_abfss_builds_the_data_lake_adapter(self) -> None:
        """Building it must not authenticate or connect -- same laziness as the engine."""
        built = build_storage("abfss://raw@acct.dfs.core.windows.net/extracts")
        assert isinstance(built, AzureDataLakeStorage)
        assert built.container == "raw"
        assert built.account_url == "https://acct.dfs.core.windows.net"
        assert built.base == "extracts"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("abfss://acct.dfs.core.windows.net/x", "missing the container"),
            ("abfss://raw@/x", "does not name both"),
        ],
    )
    def test_a_malformed_abfss_url_says_what_correct_looks_like(
        self, url: str, expected: str
    ) -> None:
        """A bad STORAGE_URL is a config mistake; the message has to be actionable."""
        with pytest.raises(StorageError, match=expected):
            build_storage(url)

    def test_abfss_without_a_base_prefix_is_the_container_root(self) -> None:
        built = build_storage("abfss://raw@acct.dfs.core.windows.net")
        assert built.base == ""

    def test_unknown_scheme_is_refused(self) -> None:
        with pytest.raises(StorageError, match="Unsupported"):
            build_storage("s3://bucket/prefix")

    def test_unconfigured_storage_raises_a_distinct_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.core.storage.get_settings", lambda: Settings(storage_url="", _env_file=None)
        )
        reset_storage_cache()
        with pytest.raises(StorageNotConfiguredError):
            get_storage()
        reset_storage_cache()

    def test_importing_the_app_does_not_build_storage(self) -> None:
        """The adapter must be lazy, like the database engine."""
        reset_storage_cache()
        assert get_storage.cache_info().currsize == 0


class TestDataLakeAdapterSpecifics:
    """Behaviour that only makes sense for the Data Lake adapter."""

    @pytest.mark.parametrize(
        ("url", "account", "container", "base"),
        [
            (
                "abfss://raw@stvziaicomnonprod.dfs.core.windows.net/extracts",
                "https://stvziaicomnonprod.dfs.core.windows.net",
                "raw",
                "extracts",
            ),
            (
                "abfss://raw@stvziaicomnonprod.dfs.core.windows.net/a/b/c",
                "https://stvziaicomnonprod.dfs.core.windows.net",
                "raw",
                "a/b/c",
            ),
            (
                "abfs://raw@stvziaicomnonprod.dfs.core.windows.net/",
                "https://stvziaicomnonprod.dfs.core.windows.net",
                "raw",
                "",
            ),
        ],
    )
    def test_url_parsing(self, url: str, account: str, container: str, base: str) -> None:
        assert parse_abfss(url) == (account, container, base)

    def test_reads_are_seekable_as_openpyxl_requires(self) -> None:
        """The reason reads are spooled rather than streamed straight through.

        An xlsx is a zip, and openpyxl seeks to the central directory before it
        parses a row. Azure's StorageStreamDownloader is forward-only, so a naive
        adapter that handed it back would work in every test here and then fail
        on the first real workbook.
        """
        store = fake_storage()
        with store.open_write("book.bin") as handle:
            handle.write(b"PK\x03\x04rest-of-a-zip")
        with store.open_read("book.bin") as handle:
            assert handle.seekable()
            assert handle.read(2) == b"PK"
            handle.seek(0)
            assert io.BytesIO(handle.read()).read(2) == b"PK"

    def test_keys_are_stored_under_the_configured_base_prefix(self) -> None:
        """The base prefix must reach the service, and must not reach the caller."""
        service = FakeDataLakeServiceClient()
        store = AzureDataLakeStorage(FAKE_URL, service_client=service)
        with store.open_write("Tables/MSEG_1.XLSX") as handle:
            handle.write(b"x")

        assert list(service.store) == ["extracts/Tables/MSEG_1.XLSX"]
        assert list(store.list()) == ["Tables/MSEG_1.XLSX"]

    def test_check_connection_fails_when_the_container_is_absent(self) -> None:
        """The first-deployment failure: wrong container, or no RBAC assignment."""
        store = AzureDataLakeStorage(
            FAKE_URL, service_client=FakeDataLakeServiceClient(container_exists=False)
        )
        with pytest.raises(StorageError, match="Storage Blob Data Reader"):
            store.check_connection()

    def test_building_the_adapter_does_not_authenticate(self) -> None:
        """No credential is resolved until a call is actually made.

        DefaultAzureCredential probes several sources and can be slow or
        interactive; doing that at import or construction time would make the
        app's start-up depend on it.
        """
        store = AzureDataLakeStorage(FAKE_URL)
        assert store._filesystem is None


class TestReadinessReportsStorage:
    """Readiness must report storage as honestly as it reports the database."""

    def test_not_configured_storage_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.core.storage.get_settings", lambda: Settings(storage_url="", _env_file=None)
        )
        reset_storage_cache()
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["storage"] == "not_configured"
        reset_storage_cache()

    def test_unavailable_storage_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Configured but unreachable must read differently from not configured.

        The deployed shape of this is the container missing, or the App Service
        identity having no role assignment on stvziaicomnonprod -- both of which
        present as a reachable account that will not serve us.
        """
        monkeypatch.setattr(
            "app.core.storage.get_settings",
            lambda: Settings(storage_url=FAKE_URL, _env_file=None),
        )
        monkeypatch.setattr(
            "app.core.storage.build_storage",
            lambda url: AzureDataLakeStorage(
                url, service_client=FakeDataLakeServiceClient(container_exists=False)
            ),
        )
        reset_storage_cache()

        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["storage"] == "unavailable"
        reset_storage_cache()
