"""3D Tiles archives (`.3tz`): a tileset as one file, read an entry at a time by its index.

A `.3tz` is a ZIP of the tileset's files, stored uncompressed, whose last entry,
`@3dtilesIndex1@`, lists every other entry's MD5 path hash and local header offset, sorted, so a
reader finds any file with one binary search and one range read. The worker writes them
(`Writer`), with a fixed date on every entry, so the same tiles make the same bytes (ING-7); the
tiler reads them (`read_index`, `locate`) a range at a time through the blob port, trusting
nothing in them: every offset and length is checked against the archive's size and a cap.
https://github.com/erikdahlstrom/3tz-specification
"""

import bisect
import hashlib
import struct
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

INDEX = "@3dtilesIndex1@"
ENTRY = struct.Struct("<16sQ")  # MD5 of the path, offset of its local file header
EPOCH = (1980, 1, 1, 0, 0, 0)  # ZIP's earliest date: entries carry no time of their own
# Reading: what's searched for the end of the central directory (its record, and the longest
# comment a ZIP may have), how much of the directory's end holds the index's entry, and the
# largest index and entry the tiler will read.
TAIL_BYTES = 22 + 65_535
DIRECTORY_TAIL_BYTES = 512
MAX_INDEX_BYTES = 16 * 1024 * 1024  # 700,000 files
MAX_ENTRY_BYTES = 512 * 1024 * 1024
END = struct.Struct("<4s4xHHIIH")  # signature, entries here and in all, directory size, offset
END64_LOCATOR = struct.Struct("<4sIQI")  # signature, disk, end64 record offset, disks
END64 = struct.Struct("<4sQHHIIQQQQ")
DIRECTORY_ENTRY = struct.Struct("<4sHHHHHHIIIHHHHHII")
LOCAL_HEADER = struct.Struct("<4sHHHHHIIIHH")
DIRECTORY_SIGNATURE, LOCAL_SIGNATURE = b"PK\x01\x02", b"PK\x03\x04"
END_SIGNATURE, END64_SIGNATURE, LOCATOR_SIGNATURE = b"PK\x05\x06", b"PK\x06\x06", b"PK\x06\x07"
STORED = 0
WIDE = 0xFFFFFFFF  # a 32-bit field whose value is in the ZIP64 extra field

type Read = Callable[[int, int], bytes]  # (offset, length) -> exactly those bytes


class Malformed(Exception):
    """The archive isn't a `.3tz` the tiler can serve from."""


@dataclass(frozen=True, slots=True)
class Index:
    """An archive's index: its sorted entries, 24 bytes each."""

    entries: bytes

    def __len__(self) -> int:
        return len(self.entries) // ENTRY.size

    def key(self, n: int) -> tuple[int, int]:
        return sort_key(self.entries[n * ENTRY.size : n * ENTRY.size + 16])

    def offsets(self, digest: bytes) -> list[int]:
        """The local header offsets of every entry hashed `digest` (collisions are adjacent)."""
        wanted = sort_key(digest)
        first = bisect.bisect_left(range(len(self)), wanted, key=self.key)
        found: list[int] = []
        for n in range(first, len(self)):  # bounded by the index
            hashed, offset = ENTRY.unpack_from(self.entries, n * ENTRY.size)
            if hashed != digest:
                break
            found.append(offset)
        return found


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


def read_index(read: Read, size: int) -> Index:
    """The index of the archive of `size` bytes that `read` reads."""
    directory_offset, directory_size = _directory(read, size)
    tail_length = min(directory_size, DIRECTORY_TAIL_BYTES)
    tail = read(directory_offset + directory_size - tail_length, tail_length)
    at = tail.rfind(DIRECTORY_SIGNATURE)
    if at < 0 or at + DIRECTORY_ENTRY.size > len(tail):
        raise Malformed("no index entry at the end of the central directory")
    fields = DIRECTORY_ENTRY.unpack_from(tail, at)
    name_length, extra_length, method = fields[10], fields[11], fields[4]
    name_at = at + DIRECTORY_ENTRY.size
    name = tail[name_at : name_at + name_length]
    if name != INDEX.encode() or method != STORED:
        raise Malformed("the last entry isn't an uncompressed index")
    extra = tail[name_at + name_length : name_at + name_length + extra_length]
    length, offset = _wide(extra, fields[9], fields[8], fields[16])
    if length % ENTRY.size or length > MAX_INDEX_BYTES:
        raise Malformed(f"an index of {length} bytes at {offset}")
    found = _data(read, size, offset, INDEX)
    if found is None:  # not reached: a name mismatch raises unless missing_ok
        raise Malformed("no index")
    return Index(read(found[0], length)) if length else Index(b"")


def locate(index: Index, read: Read, size: int, path: str) -> tuple[int, int] | None:
    """Where file `path`'s bytes are in the archive (offset and length), if it holds one."""
    for offset in index.offsets(path_hash(path)):
        found = _data(read, size, offset, path, missing_ok=True)
        if found is not None:
            return found
    return None


def _wide(extra: bytes, size: int, compressed: int, offset: int) -> tuple[int, int]:
    """A directory entry's length and local header offset, taking from its ZIP64 extra field
    (id 1) the values its 32-bit fields mark as wide, in the order ZIP gives them."""
    wide = [v == WIDE for v in (size, compressed, offset)]
    values = [size, compressed, offset]
    at = 0
    for _ in range(len(extra) // 4 + 1):  # bounded: each field is at least 4 bytes
        if at + 4 > len(extra):
            break
        kind, length = struct.unpack_from("<HH", extra, at)
        if kind == 1:
            body = extra[at + 4 : at + 4 + length]
            wanted = [i for i in range(3) if wide[i]]
            if len(body) < 8 * len(wanted):
                raise Malformed("a short ZIP64 extra field")
            for n, i in enumerate(wanted):
                values[i] = struct.unpack_from("<Q", body, 8 * n)[0]
            wide = [False, False, False]
        at += 4 + length
    if any(wide):
        raise Malformed("a wide field with no ZIP64 value")
    return values[1], values[2]


def _directory(read: Read, size: int) -> tuple[int, int]:
    """The central directory's offset and size, from the end record (ZIP64's if there is one)."""
    tail_length = min(size, TAIL_BYTES)
    tail = read(size - tail_length, tail_length)
    at = tail.rfind(END_SIGNATURE)
    if at < 0 or at + END.size > len(tail):
        raise Malformed("no end of central directory")
    _, _, _, directory_size, directory_offset, _ = END.unpack_from(tail, at)
    if WIDE in (directory_size, directory_offset):
        locator = at - END64_LOCATOR.size
        if locator < 0:
            raise Malformed("a ZIP64 archive without its locator")
        signature, _, end64, _ = END64_LOCATOR.unpack_from(tail, locator)
        if signature != LOCATOR_SIGNATURE or end64 + END64.size > size:
            raise Malformed("a malformed ZIP64 locator")
        record = END64.unpack(read(end64, END64.size))
        if record[0] != END64_SIGNATURE:
            raise Malformed("a malformed ZIP64 end record")
        directory_size, directory_offset = record[8], record[9]
    if directory_offset + directory_size > size:
        raise Malformed("a central directory past the end of the archive")
    return directory_offset, directory_size


def _data(
    read: Read, size: int, offset: int, path: str, *, missing_ok: bool = False
) -> tuple[int, int] | None:
    """The offset and length of the data after the local header at `offset`, if it is `path`'s
    and stored; None if it's another file's and `missing_ok`."""
    if offset + LOCAL_HEADER.size > size:
        raise Malformed(f"a local header past the end, at {offset}")
    header = LOCAL_HEADER.unpack(read(offset, LOCAL_HEADER.size))
    signature, method, length = header[0], header[3], header[7]
    name_length, extra_length = header[9], header[10]
    if signature != LOCAL_SIGNATURE:
        raise Malformed(f"no local header at {offset}")
    name = read(offset + LOCAL_HEADER.size, name_length)
    if name != path.encode():
        if missing_ok:
            return None
        raise Malformed(f"the entry at {offset} isn't {path}")
    start = offset + LOCAL_HEADER.size + name_length + extra_length
    if method != STORED or length == WIDE or length > MAX_ENTRY_BYTES or start + length > size:
        raise Malformed(f"{path} isn't stored whole within the archive")
    return start, length
