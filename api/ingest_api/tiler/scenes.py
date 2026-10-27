"""One item's 3D Tiles, read from its `.3tz` through the blob port a file at a time.

The scene token names the archive; a request names a file in it (`tileset.json`, `tiles/7.glb`).
Each request heads the archive, for its size and ETag, and reads that file's local header and
bytes: two ranges, at most `tilesets.MAX_ENTRY_BYTES`. An archive's index is read once per
version (its ETag) and kept, for a bounded number of archives, by the process: `Archives` is
built once, in `tiler.app`, like every adapter.
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass
from functools import partial

from ingest_api import tilesets
from ingest_api.ports import BlobNotFound, BlobStore
from ingest_api.previews import Preview

# Archives whose indexes the tiler keeps: a few hundred KiB each for the largest GLB's tileset.
CACHED_ARCHIVES = 64
MEDIA_TYPES = {".json": "application/json", ".glb": "model/gltf-binary"}
OCTETS = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class File:
    data: bytes
    media_type: str


class Archives:
    """The indexes of the archives scenes were served from, most recently used kept."""

    def __init__(self, blobs: BlobStore, capacity: int = CACHED_ARCHIVES):
        self._blobs, self._capacity = blobs, capacity
        self._indexes: OrderedDict[tuple[str, str, str], tilesets.Index] = OrderedDict()
        self._lock = threading.Lock()  # requests run on a thread pool

    def file(self, scene: Preview, path: str) -> File | None:
        """File `path` of the scene's archive, or None if it holds no such file. Raises
        BlobNotFound if the archive is gone, and `tilesets.Malformed` if it can't be read."""
        container = scene.container.value
        props = self._blobs.head(container, scene.blob)
        if props is None:
            raise BlobNotFound(f"{container}/{scene.blob}")
        read = partial(self._blobs.read_range, container, scene.blob)
        index = self._index((container, scene.blob, props.etag), read, props.size)
        found = tilesets.locate(index, read, props.size, path)
        if found is None:
            return None
        offset, length = found
        suffix = path[path.rfind(".") :] if "." in path else ""
        return File(read(offset, length) if length else b"", MEDIA_TYPES.get(suffix, OCTETS))

    def _index(self, key: tuple[str, str, str], read: tilesets.Read, size: int) -> tilesets.Index:
        with self._lock:
            if key in self._indexes:
                self._indexes.move_to_end(key)
                return self._indexes[key]
        index = tilesets.read_index(read, size)  # outside the lock: it reads storage
        with self._lock:
            self._indexes[key] = index
            if len(self._indexes) > self._capacity:
                self._indexes.popitem(last=False)
        return index
