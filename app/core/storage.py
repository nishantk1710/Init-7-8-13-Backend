"""Object storage: the port, its errors, and the adapter factory.

Every file this system reads or writes -- the SAP extract workbooks today, raw
CPI pages and model artefacts later -- goes through the ``Storage`` interface
below. No module opens a path of its own, and no module names a directory or a
container: the single source is ``Settings.storage_url``.

The adapter is chosen from the URL scheme, exactly as SQLAlchemy chooses a
dialect from ``DATABASE_URL``. Moving from a local folder to cloud storage is
therefore a config change with no branch anywhere in application code.

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
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import BinaryIO
from urllib.parse import unquote, urlparse

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


def _looks_like_a_plain_path(url: str) -> bool:
    """Whether to treat ``url`` as a filesystem path rather than parse a scheme.

    ``urlparse`` reads ``C:/data`` as scheme ``c``, so drive letters must be
    recognised before any scheme parsing happens.
    """
    return bool(WINDOWS_PATH.match(url)) or url.startswith(("/", "\\", "."))


def _local_root_from(url: str) -> str:
    """Extract a filesystem root from a plain path or a ``file://`` URL.

    Plain paths are accepted deliberately. The local extract folder is
    ``...\\KPI 02 Data Extract\\Tables`` -- spaces and all -- and making every
    developer percent-encode that into a URL buys nothing.
    """
    if _looks_like_a_plain_path(url):
        return url

    parsed = urlparse(url)
    path = unquote(parsed.path)
    # file:///D:/data parses to "/D:/data"; drop the slash before the drive letter.
    if WINDOWS_PATH.match(path.lstrip("/")):
        path = path.lstrip("/")
    if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
        # file://server/share is a UNC path.
        path = f"//{parsed.netloc}{path}"
    return path


def build_storage(url: str) -> Storage:
    """Choose an adapter from the URL scheme. The only place that mapping lives.

    Exposed (rather than private) so tests can build an adapter for a temporary
    directory without touching process-wide settings.
    """
    from app.integrations.storage.local import LocalFileSystemStorage

    if _looks_like_a_plain_path(url):
        return LocalFileSystemStorage(_local_root_from(url))

    scheme = urlparse(url).scheme.lower()

    if scheme in ("", "file"):
        return LocalFileSystemStorage(_local_root_from(url))

    if scheme in ("abfs", "abfss", "az", "https"):
        raise StorageError(
            f"STORAGE_URL scheme {scheme!r} needs the Azure Data Lake adapter, "
            "which is not written yet (app/integrations/azure/). Nothing else "
            "has to change when it lands: implement Storage, register the scheme "
            "here, and run the existing conformance suite against it."
        )

    raise StorageError(
        f"Unsupported STORAGE_URL scheme {scheme!r}. Supported today: a plain "
        "filesystem path, or file://."
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
            "STORAGE_URL is not set. Copy .env.example to .env and set it (see "
            "README, 'Storage'). No default is assumed: a storage location must "
            "never be hard-coded."
        )
    return build_storage(url)


def reset_storage_cache() -> None:
    """Drop the cached adapter. For tests that change STORAGE_URL between cases."""
    get_storage.cache_clear()
