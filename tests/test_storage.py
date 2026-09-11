"""Storage conformance suite.

Everything in ``TestStorageConformance`` is written against the ``Storage``
interface, never against a concrete adapter. Today the ``storage`` fixture yields
one implementation; when the Azure Data Lake adapter lands, add it to that
fixture's ``params`` and every test below runs against it too, with no new test
code. That is what makes "cutover is a config change" a claim the suite actually
checks rather than a hope.

Tests outside that class cover the factory, key validation and the readiness
endpoint, which are adapter-independent.
"""

from __future__ import annotations

import io
from pathlib import Path

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
from app.integrations.storage.local import LocalFileSystemStorage
from app.main import app

client = TestClient(app)


@pytest.fixture(params=["local"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Storage:
    """One configured, empty ``Storage`` per implementation under test.

    Add "adls" here once that adapter exists -- ideally pointed at a throwaway
    container -- and the whole class below covers it.
    """
    if request.param == "local":
        return LocalFileSystemStorage(tmp_path)
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
    def test_plain_windows_path_builds_local_adapter(self, tmp_path: Path) -> None:
        assert isinstance(build_storage(str(tmp_path)), LocalFileSystemStorage)

    def test_file_url_builds_local_adapter(self, tmp_path: Path) -> None:
        built = build_storage(tmp_path.as_uri())
        assert isinstance(built, LocalFileSystemStorage)
        assert built.root == tmp_path.resolve()

    def test_file_url_with_spaces_round_trips(self, tmp_path: Path) -> None:
        """The real extract folder is 'KPI 02 Data Extract' -- spaces must survive."""
        spaced = tmp_path / "KPI 02 Data Extract"
        spaced.mkdir()
        assert build_storage(spaced.as_uri()).root == spaced.resolve()

    def test_azure_scheme_names_the_missing_adapter(self) -> None:
        with pytest.raises(StorageError, match="Azure Data Lake adapter"):
            build_storage("abfss://raw@acct.dfs.core.windows.net/extracts")

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


class TestLocalAdapterSpecifics:
    """Behaviour that only makes sense for the filesystem adapter."""

    def test_missing_root_fails_fast_and_names_the_path(self) -> None:
        """A wrong STORAGE_URL must error immediately, not stall or be created.

        Regression test for a real incident: a copy of this repo ran elsewhere
        with STORAGE_URL still pointing at the original machine's OneDrive
        folder. The adapter tried to CREATE it, Windows spent minutes on the
        network path, and the suite looked frozen.
        """
        from app.core.storage import StorageError

        with pytest.raises(StorageError, match="does not exist"):
            LocalFileSystemStorage("Z:/no/such/storage/root")

    def test_root_is_created_only_when_explicitly_asked(self, tmp_path: Path) -> None:
        root = tmp_path / "not" / "there" / "yet"
        LocalFileSystemStorage(root, create=True)
        assert root.is_dir()

    def test_in_flight_temp_files_are_not_listed(self, tmp_path: Path) -> None:
        """An interrupted write leaves a .tmp sibling; it is not an object."""
        store = LocalFileSystemStorage(tmp_path)
        (tmp_path / "leftover.bin.deadbeef.tmp").write_bytes(b"x")
        assert list(store.list()) == []

    def test_check_connection_fails_when_root_disappears(self, tmp_path: Path) -> None:
        """Readiness must notice a root that vanishes after start-up.

        A network share disconnecting, or a container mount that does not come
        back, looks exactly like this.
        """
        root = tmp_path / "vanishing"
        store = LocalFileSystemStorage(root, create=True)
        root.rmdir()
        with pytest.raises(StorageError):
            store.check_connection()

    def test_openpyxl_style_file_object_read(self, tmp_path: Path) -> None:
        """Proof the handle is a real binary file object, as openpyxl requires."""
        store = LocalFileSystemStorage(tmp_path)
        with store.open_write("book.bin") as handle:
            handle.write(b"PK\x03\x04rest-of-a-zip")
        with store.open_read("book.bin") as handle:
            assert isinstance(handle.read(0), bytes)
            handle.seek(0)
            assert io.BytesIO(handle.read()).read(2) == b"PK"


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

    def test_unavailable_storage_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Configured but broken must read differently from not configured."""
        root = tmp_path / "gone"
        root.mkdir()
        monkeypatch.setattr(
            "app.core.storage.get_settings",
            lambda: Settings(storage_url=str(root), _env_file=None),
        )
        reset_storage_cache()
        get_storage()  # builds the adapter against a root that exists
        root.rmdir()  # then it disappears underneath us

        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["storage"] == "unavailable"
        reset_storage_cache()
