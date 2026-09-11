"""Local filesystem storage adapter.

Stands in for cloud object storage during development. It is a real adapter, not
a stub: the seed script reads the July SAP extracts through it, and it is held to
the same conformance suite the cloud adapter will be.

Three things it does that a naive ``open()`` wrapper would not:

* **Refuses traversal.** Every key is validated and the resolved path is checked
  to be inside the root, so a key from config or a database row cannot reach
  files elsewhere on the machine.
* **Writes atomically.** Content goes to a temporary sibling and is renamed on
  success, so an interrupted seed run leaves no truncated file that ``exists``
  would then report as present.
* **Reports Windows path-length failures for what they are.** Root plus key can
  exceed the 260-character limit, and the raw ``OSError`` for that is famously
  unhelpful.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from app.core.storage import (
    ObjectNotFoundError,
    ObjectStat,
    Storage,
    StorageError,
    validate_key,
)

# Windows refuses paths beyond this without long-path support enabled. Checked
# explicitly so the error names the cause instead of surfacing as ENOENT.
_WINDOWS_MAX_PATH = 260


class LocalFileSystemStorage(Storage):
    """``Storage`` backed by a directory tree.

    The root must already exist unless ``create=True`` -- see ``__init__``.
    """

    def __init__(self, root: str | os.PathLike[str], *, create: bool = False) -> None:
        """``create=False`` by default: a missing root is an error, not a request.

        This used to create the root automatically, which seemed convenient and
        was wrong. STORAGE_URL points at a DELIVERY of data that already exists;
        a path that is not there means the setting is wrong, and silently
        creating an empty directory turns that into "the extracts have vanished"
        much later and somewhere else.

        It was also slow in a way that looked like a hang. A copy of this
        repository was run on another machine with STORAGE_URL still pointing at
        the original machine's OneDrive folder; Windows spent minutes trying to
        resolve and create that path, and the test suite appeared to freeze with
        no output. Failing immediately, naming the path, is strictly better.

        Pass ``create=True`` where making the directory is genuinely the intent.
        """
        self._root = Path(root).expanduser().resolve()
        if create:
            self._root.mkdir(parents=True, exist_ok=True)
        elif not self._root.is_dir():
            raise StorageError(
                f"Storage root does not exist: {self._root}. "
                "Check STORAGE_URL points at the folder CONTAINING the delivery "
                "folders (see README, 'Seeding'). It is not created automatically: "
                "a missing root means the setting is wrong, not that the data "
                "should be conjured."
            )

    @property
    def root(self) -> Path:
        return self._root

    def __repr__(self) -> str:
        return f"LocalFileSystemStorage(root={str(self._root)!r})"

    # --- Key/path translation --------------------------------------------

    def _path_for(self, key: str) -> Path:
        """Resolve ``key`` to an absolute path inside the root, or raise.

        The containment check is belt and braces: ``validate_key`` already
        rejects ``..``, but symlinks can also point outward, and this is the last
        place to catch that before a file is opened.
        """
        validate_key(key)
        candidate = (self._root / key).resolve()

        if candidate != self._root and self._root not in candidate.parents:
            raise StorageError(
                f"Key {key!r} resolves outside the storage root. Refusing to "
                "read or write outside configured storage."
            )

        if os.name == "nt" and len(str(candidate)) >= _WINDOWS_MAX_PATH:
            raise StorageError(
                f"Path for key {key!r} is {len(str(candidate))} characters, at or "
                f"over the Windows {_WINDOWS_MAX_PATH}-character limit. Use a "
                "shorter STORAGE_URL root, or enable long-path support."
            )
        return candidate

    # --- Reads ------------------------------------------------------------

    @contextmanager
    def _read(self, key: str) -> Iterator[BinaryIO]:
        path = self._path_for(key)
        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"No object at key {key!r}.") from exc
        except IsADirectoryError as exc:
            raise ObjectNotFoundError(f"Key {key!r} is a directory, not an object.") from exc
        try:
            yield handle
        finally:
            handle.close()

    def open_read(self, key: str) -> AbstractContextManager[BinaryIO]:
        return self._read(key)

    # --- Writes -----------------------------------------------------------

    @contextmanager
    def _write(self, key: str) -> Iterator[BinaryIO]:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        # A unique temp name, so two concurrent writers of the same key cannot
        # corrupt each other's content -- last rename wins, neither is truncated.
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")

        handle = temp.open("wb")
        try:
            yield handle
            handle.close()
            os.replace(temp, path)
        except BaseException:
            handle.close()
            temp.unlink(missing_ok=True)
            raise
        finally:
            if not handle.closed:
                handle.close()

    def open_write(self, key: str) -> AbstractContextManager[BinaryIO]:
        return self._write(key)

    # --- Metadata ---------------------------------------------------------

    def exists(self, key: str) -> bool:
        return self._path_for(key).is_file()

    def stat(self, key: str) -> ObjectStat:
        path = self._path_for(key)
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"No object at key {key!r}.") from exc
        if not path.is_file():
            raise ObjectNotFoundError(f"Key {key!r} is a directory, not an object.")
        return ObjectStat(
            key=key,
            size=info.st_size,
            modified=datetime.fromtimestamp(info.st_mtime, tz=timezone.utc),
        )

    def list(self, prefix: str = "") -> Iterator[str]:
        base = self._root if not prefix else self._path_for(prefix.rstrip("/"))

        if base.is_file():
            yield prefix
            return
        if not base.is_dir():
            return

        keys: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file() or path.name.endswith(".tmp"):
                # In-flight atomic writes are not objects yet.
                continue
            keys.append(path.relative_to(self._root).as_posix())

        yield from sorted(keys)

    def delete(self, key: str) -> None:
        path = self._path_for(key)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"No object at key {key!r}.") from exc

    # --- Health -----------------------------------------------------------

    def check_connection(self) -> None:
        """Verify the root exists and is readable.

        Readability is what actually fails in practice -- a disconnected network
        share, a permission change, a container mount that did not come back --
        and none of those are visible from ``exists()`` on the directory alone.
        """
        if not self._root.is_dir():
            raise StorageError(f"Storage root {str(self._root)!r} is not a directory.")
        try:
            next(self._root.iterdir(), None)
        except OSError as exc:
            raise StorageError(
                f"Storage root {str(self._root)!r} is not readable: {exc}"
            ) from exc
