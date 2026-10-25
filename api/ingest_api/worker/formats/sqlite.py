"""A minimal, read-only reader for small SQLite tables, over range reads.

GeoPackage keeps its extents and CRSs in a few small tables. SQLite's own library wants a whole
file on disk; reading those tables page by page through `BlobSource` keeps extraction
header-only (ING-6): a GeoPackage of any size costs a handful of pages. Only what that needs is
here: the schema table, table B-trees, records, and overflow chains. Every walk is bounded, so a
corrupt or hostile file is `Unreadable`, never a loop. https://www.sqlite.org/fileformat2.html
"""

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass

from ingest_api.worker.extract import BlobSource, Unreadable

MAGIC = b"SQLite format 3\x00"
HEADER = 100
# B-tree page types, and the bounds every walk is held to.
TABLE_INTERIOR, TABLE_LEAF = 0x05, 0x0D
MAX_PAGES = 512  # B-tree pages visited per table
MAX_ROWS = 4096
MAX_OVERFLOW_PAGES = 64  # per value: a CRS definition is a few kilobytes
MAX_COLUMNS = 64
UTF8 = 1

type Value = int | float | str | bytes | None


@dataclass(frozen=True, slots=True)
class Database:
    source: BlobSource
    page_size: int
    usable: int  # page size less the reserved bytes at the end of each page

    @classmethod
    def open(cls, source: BlobSource) -> "Database":
        head = source.read_exact(0, HEADER)
        if not head.startswith(MAGIC):
            raise Unreadable("no SQLite header")
        (size,) = struct.unpack_from(">H", head, 16)
        page_size = 65536 if size == 1 else size
        reserved, encoding = head[20], struct.unpack_from(">I", head, 56)[0]
        if page_size < 512 or page_size & (page_size - 1) or reserved >= page_size - 480:
            raise Unreadable(f"page size {page_size}, {reserved} reserved")
        if encoding not in (0, UTF8):
            raise Unreadable(f"text encoding {encoding}")  # GeoPackage requires UTF-8
        return cls(source, page_size, page_size - reserved)

    def table(self, name: str) -> list[dict[str, Value]]:
        """The rows of table `name`, as column name to value; empty if there's no such table."""
        for row in self._rows(1):
            kind, table, _, root, sql = (list(row[1]) + [None] * 5)[:5]
            if kind == "table" and table == name and isinstance(root, int) and isinstance(sql, str):
                columns, alias = _columns(sql)
                return [_named(columns, alias, rowid, values) for rowid, values in self._rows(root)]
        return []

    def _page(self, number: int) -> tuple[bytes, int]:
        """Page `number` and where its B-tree header starts (after the file header on page 1)."""
        if number < 1 or (number - 1) * self.page_size >= self.source.size:
            raise Unreadable(f"page {number} is outside the file")
        return self.source.read_exact((number - 1) * self.page_size, self.page_size), (
            HEADER if number == 1 else 0
        )

    def _rows(self, root: int) -> Iterator[tuple[int, list[Value]]]:
        """Every (rowid, values) in the table B-tree at `root`, depth first, without recursion."""
        pending, rows = [root], 0
        for _ in range(MAX_PAGES):
            if not pending:
                return
            page, start = self._page(pending.pop())
            kind, cells = page[start], struct.unpack_from(">H", page, start + 3)[0]
            header = 12 if kind == TABLE_INTERIOR else 8
            pointers = [
                struct.unpack_from(">H", page, start + header + 2 * i)[0] for i in range(cells)
            ]
            if kind == TABLE_INTERIOR:
                children = [struct.unpack_from(">I", page, p)[0] for p in pointers]
                rightmost = struct.unpack_from(">I", page, start + 8)[0]
                pending.extend(reversed([*children, rightmost]))
                continue
            if kind != TABLE_LEAF:
                raise Unreadable(f"page type {kind} in a table B-tree")
            for pointer in pointers:
                rows += 1
                if rows > MAX_ROWS:
                    raise Unreadable(f"more than {MAX_ROWS} rows")
                yield self._cell(page, pointer)
        raise Unreadable(f"more than {MAX_PAGES} pages in one table")

    def _cell(self, page: bytes, offset: int) -> tuple[int, list[Value]]:
        size, offset = _varint(page, offset)
        rowid, offset = _varint(page, offset)
        local = self._local(size)
        payload = page[offset : offset + local]
        if local < size:
            (overflow,) = struct.unpack_from(">I", page, offset + local)
            payload += self._overflow(overflow, size - local)
        if len(payload) != size:
            raise Unreadable("a record runs past its page")
        return rowid, _record(payload)

    def _local(self, size: int) -> int:
        """How much of a payload of `size` bytes is on its own page (the format's rule)."""
        most, least = self.usable - 35, (self.usable - 12) * 32 // 255 - 23
        if size <= most:
            return size
        local = least + (size - least) % (self.usable - 4)
        return local if local <= most else least

    def _overflow(self, page_number: int, remaining: int) -> bytes:
        parts: list[bytes] = []
        for _ in range(MAX_OVERFLOW_PAGES):
            if remaining <= 0:
                return b"".join(parts)
            page, _ = self._page(page_number)
            (page_number,) = struct.unpack_from(">I", page, 0)
            chunk = page[4 : 4 + min(remaining, self.usable - 4)]
            parts.append(chunk)
            remaining -= len(chunk)
        raise Unreadable(f"a value spans more than {MAX_OVERFLOW_PAGES} overflow pages")


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    """SQLite's big-endian varint (1 to 9 bytes) at `offset`, and the offset after it."""
    value = 0
    for i in range(9):
        if offset + i >= len(data):
            raise Unreadable("a varint runs past its page")
        byte = data[offset + i]
        if i == 8:
            return (value << 8) | byte, offset + 9
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, offset + i + 1
    raise Unreadable("unreachable: a varint is at most 9 bytes")


_INTEGERS = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}


def _record(payload: bytes) -> list[Value]:
    header_size, offset = _varint(payload, 0)
    types: list[int] = []
    for _ in range(MAX_COLUMNS):
        if offset >= header_size:
            break
        serial, offset = _varint(payload, offset)
        types.append(serial)
    values: list[Value] = []
    position = header_size
    for serial in types:
        value, position = _value(payload, position, serial)
        values.append(value)
    return values


def _value(payload: bytes, at: int, serial: int) -> tuple[Value, int]:
    if serial in _INTEGERS:
        width = _INTEGERS[serial]
        return int.from_bytes(payload[at : at + width], "big", signed=True), at + width
    if serial == 7:
        return struct.unpack_from(">d", payload, at)[0], at + 8
    if serial in (0, 8, 9):
        return (None if serial == 0 else serial - 8), at
    if serial >= 12:
        width = (serial - 12) // 2
        data = payload[at : at + width]
        return (data if serial % 2 == 0 else data.decode("utf-8", "replace")), at + width
    raise Unreadable(f"serial type {serial}")


# A column definition's name: an identifier, possibly quoted.
_NAME = re.compile(r'\s*(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|(\w+))')
_CONSTRAINTS = ("constraint", "primary", "foreign", "unique", "check")


def _columns(sql: str) -> tuple[list[str], str | None]:
    """A CREATE TABLE's column names in order, and which (if any) is an alias for the rowid:
    SQLite stores NULL in that column and keeps its value as the rowid."""
    body = sql[sql.find("(") + 1 : sql.rfind(")")]
    names: list[str] = []
    alias = None
    for definition in _split(body)[:MAX_COLUMNS]:
        match = _NAME.match(definition)
        if match is None or definition.strip().lower().startswith(_CONSTRAINTS):
            continue
        name = next(group for group in match.groups() if group)
        names.append(name)
        words = definition.lower().split()
        if len(words) > 1 and words[1] == "integer" and "primary key" in definition.lower():
            alias = name
    return names, alias


def _split(body: str) -> list[str]:
    """Comma-separated definitions, not splitting inside parentheses or quotes."""
    parts: list[str] = []
    current: list[str] = []
    depth, quote = 0, ""
    for character in body:
        if quote:
            quote = "" if character == quote else quote
        elif character in "'\"`":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    parts.append("".join(current))
    return parts


def _named(
    columns: list[str], alias: str | None, rowid: int, values: list[Value]
) -> dict[str, Value]:
    row: dict[str, Value] = dict(
        zip(columns, values + [None] * (len(columns) - len(values)), strict=False)
    )
    if alias is not None:
        row[alias] = rowid
    return row
