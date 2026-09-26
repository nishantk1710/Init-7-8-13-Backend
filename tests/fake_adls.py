"""An in-memory stand-in for the Azure Data Lake SDK.

The storage conformance suite has to keep running without an Azure subscription,
a network or a credential -- it is the thing that proves the adapter behaves,
and a suite that only runs on Azure day proves nothing before Azure day.

This fake implements exactly the surface ``AzureDataLakeStorage`` touches, and
no more. Two behaviours are modelled deliberately rather than conveniently,
because they are where a real adapter goes wrong:

* ``get_paths`` raises ``ResourceNotFoundError`` for a directory that does not
  exist, the way the service does, instead of returning an empty list. An
  adapter that does not handle that turns "no files yet" into a 500.
* ``get_paths`` yields directory entries alongside files, so the adapter's
  filtering is genuinely exercised. Without them the ``is_directory`` check
  would be dead code that passes.

It is a fake, not the service. It does not prove RBAC, private endpoints or
throughput -- those are what the live check in ``python -m app.integrations.sap``
and the first deployment are for.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from azure.core.exceptions import ResourceNotFoundError


class _Path:
    """One entry in a ``get_paths`` listing."""

    def __init__(self, name: str, *, is_directory: bool = False) -> None:
        self.name = name
        self.is_directory = is_directory


class FakeFileClient:
    def __init__(
        self,
        store: dict[str, bytes],
        path: str,
        staged: dict[str, bytes] | None = None,
    ) -> None:
        self._store = store
        self._path = path
        # ADLS stages appended bytes and commits them on flush. Modelled here
        # rather than writing straight through, so a test can catch an adapter
        # that appends and forgets to flush.
        self._staged = staged if staged is not None else {}

    def exists(self) -> bool:
        return self._path in self._store

    def _require(self) -> bytes:
        try:
            return self._store[self._path]
        except KeyError:
            raise ResourceNotFoundError(f"{self._path} not found") from None

    def download_file(self) -> "FakeDownloader":
        return FakeDownloader(self._require())

    def upload_data(self, data, *, overwrite: bool = False, **_: object) -> None:
        if self._path in self._store and not overwrite:
            raise ValueError("exists and overwrite=False")
        payload = data.read() if hasattr(data, "read") else bytes(data)
        self._store[self._path] = payload

    def get_file_properties(self) -> dict:
        payload = self._require()
        # The real SDK returns a DictMixin, which is why the adapter uses .get().
        return {
            "size": len(payload),
            "last_modified": datetime.now(timezone.utc),
        }

    def create_file(self) -> None:
        self._store[self._path] = b""

    def append_data(self, data, offset: int = 0, length: int | None = None) -> None:
        current = self._staged.get(self._path, self._store.get(self._path, b""))
        if offset != len(current):
            raise ValueError(f"append offset {offset} != current size {len(current)}")
        self._staged[self._path] = current + bytes(data)

    def flush_data(self, size: int) -> None:
        staged = self._staged.pop(self._path, None)
        if staged is None:
            return
        if size != len(staged):
            raise ValueError(f"flush size {size} != staged size {len(staged)}")
        self._store[self._path] = staged

    def delete_file(self) -> None:
        self._require()
        del self._store[self._path]


class FakeDownloader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readinto(self, stream) -> int:
        stream.write(self._payload)
        return len(self._payload)

    def readall(self) -> bytes:
        return self._payload


class FakeFileSystemClient:
    def __init__(self, store: dict[str, bytes], exists: bool = True) -> None:
        self._store = store
        self._exists = exists
        # Shared across every file client this filesystem hands out, so a
        # staged append survives the adapter fetching a fresh client.
        self._staged: dict[str, bytes] = {}

    def exists(self) -> bool:
        return self._exists

    def get_file_client(self, path: str) -> FakeFileClient:
        return FakeFileClient(self._store, path, self._staged)

    def get_paths(self, path: str | None = None, recursive: bool = True) -> Iterator[_Path]:
        root = (path or "").strip("/")
        prefix = f"{root}/" if root else ""

        matching = [key for key in self._store if key.startswith(prefix)]

        if root and not matching:
            # The service's behaviour for a directory that was never created.
            raise ResourceNotFoundError(f"{root} not found")

        directories: set[str] = set()
        for key in matching:
            remainder = key[len(prefix) :]
            parts = remainder.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                directories.add(prefix + "/".join(parts[:depth]))

        entries = [_Path(name, is_directory=True) for name in sorted(directories)]
        entries += [_Path(key) for key in sorted(matching)]

        if not recursive:
            entries = [e for e in entries if "/" not in e.name[len(prefix) :]]

        yield from entries


class FakeDataLakeServiceClient:
    """Stands in for ``DataLakeServiceClient``. One container, one dict."""

    def __init__(self, container_exists: bool = True) -> None:
        self.store: dict[str, bytes] = {}
        self._container_exists = container_exists

    def get_file_system_client(self, container: str) -> FakeFileSystemClient:
        return FakeFileSystemClient(self.store, self._container_exists)
