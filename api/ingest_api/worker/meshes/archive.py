"""3D Tiles archives (`.3tz`): a tileset as one file, read an entry at a time by its index.

A `.3tz` is a ZIP of the tileset's files, stored uncompressed, whose last entry,
`@3dtilesIndex1@`, lists every other entry's MD5 path hash and local header offset, sorted, so a
reader finds any file with one binary search and one range read. Entries carry a fixed date, so
the same tiles make the same bytes (ING-7).
https://github.com/erikdahlstrom/3tz-specification
"""

import hashlib
import struct
import zipfile
from pathlib import Path
from types import TracebackType
from typing import Self

INDEX = "@3dtilesIndex1@"
ENTRY = struct.Struct("<16sQ")  # MD5 of the path, offset of its local file header
EPOCH = (1980, 1, 1, 0, 0, 0)  # ZIP's earliest date: entries carry no time of their own


def path_hash(path: str) -> bytes:
    """The index's key for `path`: the MD5 of it with forward slashes and no leading one."""
    return hashlib.md5(path.replace("\\", "/").lstrip("/").encode(), usedforsecurity=False).digest()


def sort_key(digest: bytes) -> tuple[int, int]:
    """The index's order: the hash as two little-endian 64-bit integers, first eight bytes first."""
    return int.from_bytes(digest[:8], "little"), int.from_bytes(digest[8:], "little")


class Writer:
    """A `.3tz` being written; closing it writes the index. A context manager."""

    def __init__(self, path: Path):
        self._zip = zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True)
        self._entries: list[tuple[bytes, int]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if kind is None:
            self._write_index()
        self._zip.close()

    def add(self, path: str, data: bytes) -> None:
        info = _info(path)
        self._zip.writestr(info, data)
        self._entries.append((path_hash(path), info.header_offset))

    def _write_index(self) -> None:
        entries = sorted(self._entries, key=lambda entry: sort_key(entry[0]))
        self._zip.writestr(_info(INDEX), b"".join(ENTRY.pack(*e) for e in entries))


def _info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=EPOCH)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    return info
