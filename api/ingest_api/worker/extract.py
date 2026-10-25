"""Header-only metadata extraction (ING-6): what a promoted original says about itself.

Formats are read through `BlobSource`, a view of the blob store that fetches
aligned blocks by range and refuses to fetch more than `MAX_EXTRACT_BYTES` in total. Header
readers (`worker.formats`) seek straight to the parts they need, so a multi-GB file costs a few
range requests. A format that needs more than the budget is cataloged without metadata
(`over-budget`); bytes that don't parse are `unreadable`. Neither fails the upload: the file is
still kept and searchable by name. A storage failure while reading is retried like any other.
"""

import io
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox, Json, Metadata, Status
from ingest_api.ports import BlobStore, StorageError

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


class BlobSource:
    """One blob, read by range in aligned blocks, fetching at most MAX_EXTRACT_BYTES in all.

    Format libraries read it from native code, which may swallow exceptions; `failure` keeps
    the first storage or budget error so `extract` can act on what really happened.
    """

    def __init__(self, store: BlobStore, container: str, name: str, size: int):
        self._store, self._container, self.name = store, container, name
        self.size = size
        self.fetched = 0
        self.failure: StorageError | BudgetExceeded | None = None
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
        length = min(BLOCK_BYTES, self.size - offset)
        if self.fetched + length > MAX_EXTRACT_BYTES:
            self.failure = self.failure or BudgetExceeded(self.name)
            raise self.failure
        try:
            block = self._store.read_range(self._container, self.name, offset, length)
        except StorageError as exc:
            self.failure = self.failure or exc
            raise
        self.fetched += length
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
        read looks like the end of the file, and `extract` finds the cause in `failure`."""
        try:
            data = self._source.read(self._position, len(buffer))
        except (StorageError, BudgetExceeded, Unreadable):
            return 0
        memoryview(buffer).cast("B")[: len(data)] = data
        self._position += len(data)
        return len(data)


type Reader = Callable[[BlobSource], Found]


def extract(reader: Reader | None, source: BlobSource) -> Extraction:
    """Runs `reader` over `source`. Raises StorageError if storage failed under it, so the job
    is retried rather than cataloged without metadata."""
    if reader is None:
        return _without("unsupported", source)
    found: Found | None
    try:
        found = reader(source)
    except (Unreadable, BudgetExceeded, StorageError):
        found = None
    # Checked even after a success: a library may have swallowed the failure and carried on.
    if isinstance(source.failure, StorageError):
        raise source.failure
    if isinstance(source.failure, BudgetExceeded):
        return _without("over-budget", source)
    if found is None:
        return _without("unreadable", source)
    metadata = Metadata("extracted", source.fetched, found.properties, found.bbox, found.crs)
    return Extraction(metadata, found.kind)


def _without(status: Status, source: BlobSource) -> Extraction:
    return Extraction(Metadata(status, source.fetched), None)
