"""Header-only metadata extraction (ING-6): what a promoted original says about itself.

A format reader parses hostile bytes in native code (GDAL, Arrow), so it runs in a `Sandbox`:
in production a child process with time and memory limits (`adapters.process_sandbox`), so a
reader that crashes, hangs or exhausts memory fails that one extraction, not the worker (ING-8).
The reader sees the blob through `BlobSource`, a file-like view that fetches aligned blocks;
every fetch is served by the worker's `Budget`, which refuses to fetch more than
`MAX_EXTRACT_BYTES` in total and keeps the first storage or budget failure. Header readers
(`worker.formats`) seek straight to the parts they need, so a multi-GB file costs a few range
requests. A format that needs more than the budget is cataloged without metadata
(`over-budget`); bytes that don't parse are `unreadable`; a reader that dies is `failed`. None of
them fails the upload: the file is still kept and searchable by name. A storage failure while
reading is retried like any other.
"""

import io
import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox, Json, Metadata, Status
from ingest_api.ports import StorageError

log = logging.getLogger("ingest.worker")

# The most one extraction may fetch from storage, whatever the file's size (ING-6).
MAX_EXTRACT_BYTES = 4 * 1024 * 1024
# Fetches are whole aligned blocks; the most recent ones are kept for nearby reads.
BLOCK_BYTES = 64 * 1024
MAX_CACHED_BLOCKS = 16


class Unreadable(Exception):
    """The bytes aren't the format their signature claims, or a header is malformed."""


class BudgetExceeded(Exception):
    """Reading this header would fetch more than MAX_EXTRACT_BYTES."""


@dataclass(frozen=True, slots=True)
class Found:
    """What a format reader learned from a header."""

    properties: Mapping[str, Json] = field(default_factory=dict[str, Json])
    bbox: Bbox | None = None
    crs: str | None = None
    kind: Kind | None = None  # a refinement of the classifier's kind (GeoParquet is vector)


@dataclass(frozen=True, slots=True)
class Extraction:
    metadata: Metadata
    kind: Kind | None  # the reader's refinement of the classifier's kind, if any


type Fetch = Callable[[int, int], bytes]


class Budget:
    """The worker's gate on one blob: every byte a reader gets passes through `fetch`, which is
    the one place the budget is enforced (ING-6). It runs in the worker, never in the sandbox,
    so nothing a file makes a reader do can overspend it or hide a failure from it."""

    def __init__(self, read_range: Fetch, name: str, size: int):
        """`read_range(offset, length)` reads the blob from storage, e.g. a `BlobStore.read_range`
        with the blob's container and name bound."""
        self._read_range, self.name, self.size = read_range, name, size
        self.fetched = 0
        self.failure: StorageError | BudgetExceeded | None = None

    def fetch(self, offset: int, length: int) -> bytes:
        """`length` bytes at `offset`, counted against the budget."""
        if offset < 0 or length < 0 or offset + length > self.size:
            raise Unreadable(f"wants bytes {offset}+{length} of a {self.size}-byte file")
        if self.fetched + length > MAX_EXTRACT_BYTES:
            self.failure = self.failure or BudgetExceeded(self.name)
            raise self.failure
        try:
            data = self._read_range(offset, length)
        except StorageError as exc:
            self.failure = self.failure or exc
            raise
        self.fetched += length
        return data


class BlobSource:
    """One blob as a reader sees it: read by range in aligned blocks, the most recent of which
    are kept for nearby reads. Blocks come from `fetch`, which is the worker's `Budget` (or,
    from a sandbox, a request to it)."""

    def __init__(self, fetch: Fetch, name: str, size: int):
        self._fetch, self.name, self.size = fetch, name, size
        self._blocks: OrderedDict[int, bytes] = OrderedDict()

    def read(self, offset: int, length: int) -> bytes:
        """Up to `length` bytes at `offset`; fewer at the end of the blob."""
        end = min(offset + length, self.size)
        parts: list[bytes] = []
        position = offset
        while position < end:  # bounded: each pass advances by at least one byte
            index, start = divmod(position, BLOCK_BYTES)
            chunk = self._block(index)[start : start + end - position]
            if not chunk:
                raise Unreadable(f"{self.name} is shorter than its {self.size} bytes")
            parts.append(chunk)
            position += len(chunk)
        return b"".join(parts)

    def read_exact(self, offset: int, length: int) -> bytes:
        """Exactly `length` bytes at `offset`, for header readers; Unreadable past the end."""
        if offset < 0 or length < 0 or offset + length > self.size:
            raise Unreadable(f"wants bytes {offset}+{length} of a {self.size}-byte file")
        return self.read(offset, length)

    def open(self) -> "SourceFile":
        """A new file object over the blob, with its own position."""
        return SourceFile(self)

    def _block(self, index: int) -> bytes:
        if index in self._blocks:
            self._blocks.move_to_end(index)
            return self._blocks[index]
        offset = index * BLOCK_BYTES
        block = self._fetch(offset, min(BLOCK_BYTES, self.size - offset))
        self._blocks[index] = block
        if len(self._blocks) > MAX_CACHED_BLOCKS:
            self._blocks.popitem(last=False)
        return block


class SourceFile(io.RawIOBase):
    """A read-only, seekable file over a `BlobSource`, for libraries that want one."""

    def __init__(self, source: BlobSource):
        super().__init__()
        self._source = source
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._source.size}
        position = base[whence] + offset
        if position < 0:
            raise ValueError("negative seek position")
        self._position = position
        return position

    def readinto(self, buffer: bytearray | memoryview) -> int:  # pyright: ignore[reportIncompatibleMethodOverride] - io's stub takes any buffer
        """Never raises: GDAL aborts the process when a Python read callback does. A failed
        read looks like the end of the file, and the budget keeps the cause."""
        try:
            data = self._source.read(self._position, len(buffer))
        except (StorageError, BudgetExceeded, Unreadable):
            return 0
        memoryview(buffer).cast("B")[: len(data)] = data
        self._position += len(data)
        return len(data)


type Reader = Callable[[BlobSource], Found]


@dataclass(frozen=True, slots=True)
class Failed:
    """The reader crashed, ran out of time or memory, or raised something it shouldn't."""

    reason: str


class Sandbox(Protocol):
    """Where readers run: a child process in production, inline in some tests."""

    def run(self, reader: Reader, budget: Budget) -> Found | Failed | None:
        """What `reader` found in the budget's blob; None if the bytes don't parse."""
        ...


def read_header(reader: Reader, fetch: Fetch, name: str, size: int) -> Found | None:
    """Runs `reader` over the blob `fetch` serves: a sandbox's job, wherever it runs."""
    try:
        return reader(BlobSource(fetch, name, size))
    except (Unreadable, BudgetExceeded, StorageError):
        return None


def extract(reader: Reader | None, budget: Budget, sandbox: Sandbox) -> Extraction:
    """Runs `reader` in `sandbox` over the budget's blob. Raises StorageError if storage failed
    under it, so the job is retried rather than cataloged without metadata."""
    if reader is None:
        return _without("unsupported", budget)
    found = sandbox.run(reader, budget)
    # The budget's record wins over the reader's: a library may have swallowed the failure and
    # carried on, and a sandboxed reader can't be trusted to report it.
    if isinstance(budget.failure, StorageError):
        raise budget.failure
    if isinstance(budget.failure, BudgetExceeded):
        return _without("over-budget", budget)
    if isinstance(found, Failed):
        log.warning("reader for %s failed: %s", budget.name, found.reason)
        return _without("failed", budget)
    if found is None:
        return _without("unreadable", budget)
    metadata = Metadata("extracted", budget.fetched, found.properties, found.bbox, found.crs)
    return Extraction(metadata, found.kind)


def _without(status: Status, budget: Budget) -> Extraction:
    return Extraction(Metadata(status, budget.fetched), None)
