"""Object storage: the port, its errors, and the adapter factory.

Every file this system reads or writes -- the SAP extract workbooks today, raw
CPI pages and model artefacts later -- goes through the ``Storage`` interface
below. No module opens a path of its own, and no module names a directory or a
container: the single source is ``Settings.storage_url``.

The adapter is chosen from the URL scheme, exactly as SQLAlchemy chooses a
dialect from ``DATABASE_URL``. Today that resolves to Azure Data Lake and
nothing else: the local-folder adapter was a stand-in until VZI's storage
account existed, and it has been removed now that it does. The seam remains, so
a second backing store is an adapter plus one line in ``build_storage`` -- but
there is deliberately no longer a local path to fall back onto and no way to
run against one by accident.

Two properties this interface exists to guarantee:

**Streaming, never bytes.** ``open_read``/``open_write`` hand back file objects.
The largest extract is 143 MB; a ``read() -> bytes`` interface would hold that in
memory locally, and download it whole from cloud storage before parsing a single
row.

**Keys are POSIX-ish strings, never paths.** ``extracts/MSEG_1.XLSX`` -- forward
slashes, no drive letters, no backslashes. Only a local adapter may translate a
key into a filesystem path. A ``Path`` leaking through this interface works fine
on Windows and then silently creates wrongly-named blobs on Azure.
"""

from __future__ import annotations

import hashlib
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import BinaryIO
from urllib.parse import urlparse

from app.core.config import get_settings

# --- Errors ---------------------------------------------------------------
#
# Mirrors app.core.db: "not configured" is a distinct type from "configured but
# failing", because readiness reports them separately and they need different
# fixes.


class StorageError(RuntimeError):
    """Base class for every storage failure raised by this package."""


class StorageNotConfiguredError(StorageError):
    """STORAGE_URL is empty, so there is no storage to talk to."""


class ObjectNotFoundError(StorageError):
    """The key does not exist."""


class InvalidKeyError(StorageError):
    """The key is not a legal storage key -- see ``validate_key``."""


# --- Keys -----------------------------------------------------------------

WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_ILLEGAL_SEGMENTS = {"", ".", ".."}


def _looks_like_a_local_path(url: str) -> bool:
    """Whether ``url`` is a filesystem path rather than a storage URL.

    Kept after the local adapter was removed so a leftover ``STORAGE_URL`` gets
    an explanation instead of "unsupported scheme 'c'" -- ``urlparse`` reads
    ``C:/data`` as scheme ``c``, which is a genuinely baffling thing to be told.
    """
    return bool(WINDOWS_PATH.match(url)) or url.startswith(("/", "\\", ".", "file://"))


def validate_key(key: str) -> str:
    """Return ``key`` if it is a legal storage key, else raise ``InvalidKeyError``.

    Rejects, in order of how much damage each would do:

    * ``..`` segments -- a key read from config or a database row must not be
      able to escape the configured root and reach arbitrary files.
    * backslashes -- see the module docstring; these break the cloud adapter
      silently rather than loudly, which is the worst kind of break.
    * absolute keys and empty segments -- ambiguous across adapters.
    """
    if not isinstance(key, str) or not key:
        raise InvalidKeyError("Key must be a non-empty string.")
    if "\\" in key:
        raise InvalidKeyError(
            f"Key {key!r} contains a backslash. Storage keys always use forward "
            "slashes; only a local adapter converts them to filesystem paths."
        )
    if key.startswith("/"):
        raise InvalidKeyError(f"Key {key!r} must be relative, not absolute.")
    for segment in key.split("/"):
        if segment in _ILLEGAL_SEGMENTS:
            raise InvalidKeyError(
                f"Key {key!r} contains an empty, '.' or '..' segment. Path "
                "traversal is refused rather than resolved."
            )
    return key


# --- The port -------------------------------------------------------------


@dataclass(frozen=True)
class ObjectStat:
    """What every adapter can report about an object without reading it."""

    key: str
    size: int
    modified: datetime | None = None


class Storage(ABC):
    """Blob-style storage. One implementation per backing platform.

    Deliberately small. Every method here has to be implemented, and proven by
    the shared conformance suite, once per adapter.
    """

    @abstractmethod
    def open_read(self, key: str) -> AbstractContextManager[BinaryIO]:
        """Open ``key`` for streaming binary reading.

        Raises ``ObjectNotFoundError`` if it does not exist.
        """

    @abstractmethod
    def open_write(self, key: str) -> AbstractContextManager[BinaryIO]:
        """Open ``key`` for streaming binary writing, replacing any existing object.

        The write is committed when the context exits normally and discarded if
        the body raises, so a failed run never leaves a truncated object that
        ``exists`` then cheerfully reports as present.
        """

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether ``key`` currently holds an object."""

    @abstractmethod
    def stat(self, key: str) -> ObjectStat:
        """Size and modification time. Raises ``ObjectNotFoundError`` if absent."""

    @abstractmethod
    def list(self, prefix: str = "") -> Iterator[str]:
        """Yield keys under ``prefix``, recursively, in sorted order.

        Sorted so callers and tests are deterministic. An adapter whose platform
        returns an arbitrary order must sort before yielding.
        """

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove ``key``. Raises ``ObjectNotFoundError`` if absent."""

    @abstractmethod
    def check_connection(self) -> None:
        """Prove the backing store is reachable and usable. Raises on failure.

        Used by the readiness endpoint, and the reason that endpoint can report
        storage as honestly as it reports the database.
        """


# --- Helpers --------------------------------------------------------------

_HASH_CHUNK = 1024 * 1024


def sha256_of(storage: Storage, key: str) -> str:
    """Hex SHA-256 of a stored object, read in chunks.

    Fills ``IngestionRun.source_sha256``, so a re-run can tell "the same extract
    again" from "a new extract delivered under the same file name". Streams, so
    a 143 MB workbook costs 1 MB of memory.
    """
    digest = hashlib.sha256()
    with storage.open_read(key) as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# --- Factory --------------------------------------------------------------


def build_storage(url: str) -> Storage:
    """Choose an adapter from the URL scheme. The only place that mapping lives.

    Exposed (rather than private) so tests can build an adapter without touching
    process-wide settings.
    """
    if _looks_like_a_local_path(url):
        # ``ALLOW_NON_AZURE_SQL=1`` lifts this gate too -- see app.core.db for
        # the flag's origin. It exists there because Azure SQL sits behind a
        # private endpoint only the VNet can reach; the same is true of the
        # Azure Data Lake account, so CI and local development need *some*
        # storage location to prove seeding and app code work. Reusing one
        # flag rather than adding a second keeps "this run is against
        # non-Azure infrastructure" a single on/off switch instead of two that
        # could disagree.
        if os.environ.get("ALLOW_NON_AZURE_SQL") == "1":
            from app.integrations.storage.local import LocalFileSystemStorage

            return LocalFileSystemStorage(url)
        raise StorageError(
            f"STORAGE_URL {url!r} is a local path. This system now runs against "
            "Azure Data Lake only -- the local folder was a stand-in until VZI's "
            "storage account existed, and it does. Set STORAGE_URL to "
            "abfss://<container>@stvziaicomnonprod.dfs.core.windows.net/<path>. "
            "See README, 'Storage'. Set ALLOW_NON_AZURE_SQL=1 to use a local "
            "folder anyway (CI/local dev only -- never in a deployed "
            "environment)."
        )

    scheme = urlparse(url).scheme.lower()

    if scheme in ("abfs", "abfss"):
        from app.integrations.storage.adls import AzureDataLakeStorage

        return AzureDataLakeStorage(url)

    raise StorageError(
        f"Unsupported STORAGE_URL scheme {scheme!r}. Supported: "
        "abfss://<container>@<account>.dfs.core.windows.net/<path>."
    )


@lru_cache
def get_storage() -> Storage:
    """The process-wide storage adapter, built on first use.

    Lazy for the same reason the database engine is: importing the application
    must not require storage to be configured, or the liveness endpoint and the
    test suite stop working on a machine that has none.
    """
    url = get_settings().storage_url
    if not url:
        raise StorageNotConfiguredError(
            "STORAGE_URL is not set. Expected "
            "abfss://<container>@stvziaicomnonprod.dfs.core.windows.net/<path> "
            "(see README, 'Storage'). No default is assumed: a storage location "
            "must never be hard-coded."
        )
    return build_storage(url)


def reset_storage_cache() -> None:
    """Drop the cached adapter. For tests that change STORAGE_URL between cases."""
    get_storage.cache_clear()
