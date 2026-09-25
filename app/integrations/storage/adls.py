"""Azure Data Lake Storage Gen2 adapter.

Implements the ``Storage`` port against VZI's ``stvziaicomnonprod`` account, so
``STORAGE_URL=abfss://<container>@<account>.dfs.core.windows.net/<path>`` works
wherever storage is read or written. Nothing outside this file knows it is Azure.

**Authentication is by managed identity, not a key.** ``DefaultAzureCredential``
resolves to the App Service's identity when deployed and to the developer's
``az login`` session locally, so no secret is stored, rotated or leaked. An
account key is accepted via ``AZURE_STORAGE_ACCOUNT_KEY`` for the case where
neither is available, but it is the fallback, not the design.

**Reads are seekable, and that is not incidental.** ``open_read`` has to hand
back something ``openpyxl`` can use, and an xlsx is a zip -- the reader seeks to
the central directory before it parses a single row. Azure's
``StorageStreamDownloader`` is forward-only, so every read here lands in a
``SpooledTemporaryFile``: held in memory up to a threshold, spilled to disk
beyond it. Memory therefore stays flat on a 143 MB workbook, which is the same
property the local adapter has, reached a different way.

The private endpoints (``pe-stvziaicomnonprod-blob`` and ``-dfs``) mean this
traffic never leaves the VNet once the App Service has VNet integration. That is
infrastructure, not code: this adapter is identical either way.

**Known cost, accepted deliberately.** Spooling means each ``open_read`` fetches
the whole object before the caller sees a byte, and the seed opens every file
twice -- once to hash it for the idempotency check, once to read its rows. A
full seed therefore transfers roughly 1.6 GB rather than 800 MB. Inside the VNet
that is seconds, and the alternative -- caching downloads across calls, or
hashing while parsing -- adds state and coupling to save time that is not
currently scarce. Revisit if the seed ever runs across a slow link.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import BinaryIO
from urllib.parse import urlparse

from azure.core.exceptions import ResourceNotFoundError

from app.core.logging import get_logger
from app.core.storage import (
    ObjectNotFoundError,
    ObjectStat,
    Storage,
    StorageError,
    validate_key,
)

logger = get_logger(__name__)

# Bytes held in memory before a transfer spills to disk. One chunk of a large
# workbook, not the workbook: big enough that the 27 small extracts never touch
# the filesystem, small enough that several concurrent reads cannot exhaust an
# App Service instance's memory.
SPOOL_MAX_BYTES = 32 * 1024 * 1024

# Transfer chunk. Matches the SDK's own default block size.
CHUNK_BYTES = 4 * 1024 * 1024


def parse_abfss(url: str) -> tuple[str, str, str]:
    """Split ``abfss://container@account.dfs.core.windows.net/base`` into parts.

    Returns ``(account_url, container, base_prefix)``. Raises ``StorageError``
    with the expected shape rather than an opaque parse failure -- a malformed
    STORAGE_URL is a configuration mistake someone has to correct, so the message
    has to say what correct looks like.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("abfs", "abfss"):
        raise StorageError(f"Not an abfss:// URL: {url!r}")

    if "@" not in parsed.netloc:
        raise StorageError(
            f"STORAGE_URL {url!r} is missing the container. Expected "
            "abfss://<container>@<account>.dfs.core.windows.net/<path>."
        )

    container, _, host = parsed.netloc.partition("@")
    if not container or not host:
        raise StorageError(
            f"STORAGE_URL {url!r} does not name both a container and an account."
        )

    # The SDK is addressed at the account, not the dfs endpoint spelling, and
    # always over https -- abfss is a scheme name, not a transport.
    account_url = f"https://{host}"
    base = parsed.path.strip("/")
    return account_url, container, base


class AzureDataLakeStorage(Storage):
    """``Storage`` over one container, optionally rooted at a prefix."""

    def __init__(
        self,
        url: str,
        *,
        credential: object | None = None,
        service_client: object | None = None,
    ) -> None:
        self.account_url, self.container, self.base = parse_abfss(url)
        self._url = url
        self._explicit_credential = credential
        # Injectable so the conformance suite can run against a fake or against
        # Azurite without reaching the real account.
        self._service = service_client
        self._filesystem = None

    # --- Wiring -----------------------------------------------------------

    def _credential(self) -> object:
        if self._explicit_credential is not None:
            return self._explicit_credential

        from app.core.config import get_settings

        key = get_settings().azure_storage_account_key
        if key:
            logger.info("ADLS: authenticating with an account key")
            return key

        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise StorageError(
                "azure-identity is not installed, so the Data Lake adapter cannot "
                "authenticate. Install requirements.txt."
            ) from exc

        logger.info("ADLS: authenticating with DefaultAzureCredential")
        return DefaultAzureCredential()

    def _client(self):  # noqa: ANN202 - SDK type, imported lazily
        """The filesystem (container) client, built once on first use."""
        if self._filesystem is not None:
            return self._filesystem

        service = self._service
        if service is None:
            try:
                from azure.storage.filedatalake import DataLakeServiceClient
            except ImportError as exc:  # pragma: no cover - declared dependency
                raise StorageError(
                    "azure-storage-file-datalake is not installed, so "
                    f"STORAGE_URL={self._url!r} cannot be served. Install "
                    "requirements.txt."
                ) from exc

            service = DataLakeServiceClient(
                account_url=self.account_url, credential=self._credential()
            )

        self._filesystem = service.get_file_system_client(self.container)
        return self._filesystem

    def _path_of(self, key: str) -> str:
        """Absolute path inside the container for a validated key."""
        validate_key(key)
        return f"{self.base}/{key}" if self.base else key

    @contextmanager
    def _translating_errors(self, key: str) -> Iterator[None]:
        """Map the SDK's not-found onto ours, leaving other failures alone."""
        try:
            yield
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(f"{key} does not exist in {self._url}") from exc

    # --- The port ---------------------------------------------------------

    @contextmanager
    def open_read(self, key: str) -> Iterator[BinaryIO]:
        path = self._path_of(key)
        buffer = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
        try:
            with self._translating_errors(key):
                downloader = self._client().get_file_client(path).download_file()
                # readinto() streams in chunks; read() would build one bytes
                # object the size of the whole workbook.
                downloader.readinto(buffer)
            buffer.seek(0)
            yield buffer  # type: ignore[misc]
        finally:
            buffer.close()

    @contextmanager
    def open_write(self, key: str) -> Iterator[BinaryIO]:
        path = self._path_of(key)
        buffer = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
        try:
            yield buffer  # type: ignore[misc]
            # Only reached when the body completed. A raising body skips the
            # upload entirely, so a failed write leaves no object and never
            # truncates an existing one -- the contract the port specifies and
            # the conformance suite checks.
            buffer.seek(0)
            self._client().get_file_client(path).upload_data(
                buffer, overwrite=True, chunk_size=CHUNK_BYTES
            )
        finally:
            buffer.close()

    def append(self, key: str, data: bytes) -> int:
        """Append at the current end of file, using ADLS Gen2's own append API.

        ``append_data`` stages bytes at an offset and ``flush_data`` commits the
        new length; neither transfers what is already stored. The offset has to
        be the current size, so it is read from the file properties rather than
        tracked locally -- two workers appending to one file would otherwise
        both believe they were at the end and one would overwrite the other.
        """
        path = self._path_of(key)
        client = self._client().get_file_client(path)

        if client.exists():
            size = int(client.get_file_properties().get("size") or 0)
        else:
            client.create_file()
            size = 0

        if not data:
            return size

        client.append_data(data, offset=size, length=len(data))
        client.flush_data(size + len(data))
        return size + len(data)

    def exists(self, key: str) -> bool:
        return self._client().get_file_client(self._path_of(key)).exists()

    def stat(self, key: str) -> ObjectStat:
        path = self._path_of(key)
        with self._translating_errors(key):
            properties = self._client().get_file_client(path).get_file_properties()

        modified = properties.get("last_modified")
        if modified is not None and not isinstance(modified, datetime):
            modified = None

        return ObjectStat(
            key=key, size=int(properties.get("size") or 0), modified=modified
        )

    def list(self, prefix: str = "") -> Iterator[str]:
        root = self._path_of(prefix) if prefix else self.base
        strip = len(self.base) + 1 if self.base else 0

        names: list[str] = []
        try:
            # get_paths returns a lazy pager, so a missing directory raises on
            # ITERATION, not on the call. Wrapping only the call would look
            # correct and catch nothing -- which is exactly what it did.
            for path in self._client().get_paths(path=root or None, recursive=True):
                if getattr(path, "is_directory", False):
                    continue
                names.append(path.name[strip:])
        except ResourceNotFoundError:
            # A prefix with nothing under it is an empty listing, not an error.
            # Before anything is uploaded the container's root is exactly this,
            # so treating it as a failure would break the first run.
            return

        # Sorted because the port promises it and callers compare listings.
        yield from sorted(name for name in names if name)

    def delete(self, key: str) -> None:
        path = self._path_of(key)
        with self._translating_errors(key):
            self._client().get_file_client(path).delete_file()

    def check_connection(self) -> None:
        """Prove the container is reachable and we are allowed to read it.

        Listing one entry is the cheapest call that exercises DNS, the private
        endpoint, the credential and the RBAC assignment together -- which is
        exactly the set of things that can be wrong on the first deployment.
        """
        client = self._client()
        if not client.exists():
            raise StorageError(
                f"Container {self.container!r} not found at {self.account_url}. "
                "Check STORAGE_URL, and that the identity has "
                "'Storage Blob Data Reader' on the account."
            )
        try:
            paths = client.get_paths(path=self.base or None, recursive=False)
            next(iter(paths), None)
        except ResourceNotFoundError:
            # The container is reachable and we are authorised; the base prefix
            # just has nothing in it yet. That is readiness, not a failure --
            # reporting otherwise would make a freshly created container look
            # broken until the first upload.
            logger.info("ADLS: %s is reachable and empty", self._url)

    def __repr__(self) -> str:
        return (
            f"AzureDataLakeStorage({self.account_url}/{self.container}"
            f"{'/' + self.base if self.base else ''})"
        )
