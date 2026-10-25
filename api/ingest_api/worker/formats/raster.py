"""Rasters (GeoTIFF and COG, PNG, JPEG and anything else GDAL reads): size, bands, CRS and
footprint, from the header alone.

GDAL reads through rasterio's Python-filesystem plugin, so every byte it asks for comes through
`BlobSource` and counts against the budget; it never gets a URL or a credential of its own.
Opening a dataset reads its header and IFDs; no pixel data is touched. A TIFF whose blocks run
past the end of the file is `unreadable`, however good its header looks.
"""

import math
import warnings
from typing import override

import rasterio
from rasterio.abc import FileContainer
from rasterio.errors import NotGeoreferencedWarning, RasterioError

from ingest_api.items.metadata import Json
from ingest_api.worker.extract import BlobSource, Found, SourceFile, Unreadable
from ingest_api.worker.formats import crs

# The single path GDAL is given; the container below serves it.
PATH = "blob://source"
MAX_BANDS = 64


class _OneBlob(FileContainer):
    """Serves the source blob, and nothing else, to GDAL. GDAL may open it several times and
    probes for sidecar files (`.aux.xml`, `.ovr`), which don't exist."""

    def __init__(self, source: BlobSource):
        self._source = source

    @override
    def open(self, path: str, mode: str = "r", **kwds: object) -> SourceFile:
        if path != PATH or "w" in mode or "a" in mode:
            raise FileNotFoundError(path)
        return self._source.open()

    @override
    def isfile(self, path: str) -> bool:
        return path == PATH

    @override
    def isdir(self, path: str) -> bool:
        return False

    @override
    def ls(self, path: str) -> list[str]:
        return []

    @override
    def mtime(self, path: str) -> int:
        return 0

    @override
    def rm(self, path: str) -> None:
        raise PermissionError(path)

    @override
    def size(self, path: str) -> int:
        if path != PATH:
            raise FileNotFoundError(path)
        return self._source.size


def read(source: BlobSource) -> Found:
    try:
        with warnings.catch_warnings():
            # An image without georeferencing is normal here (a PNG): it just has no footprint.
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            dataset = rasterio.open(PATH, opener=_OneBlob(source))
        with dataset:
            _check_complete(dataset, source.size)
            return _found(dataset)
    except RasterioError as exc:
        raise Unreadable(str(exc)) from exc


def _check_complete(dataset: rasterio.DatasetReader, size: int) -> None:
    """A TIFF's header lists where every block is, so a file cut short (a transfer committed
    before its end) shows in the header alone: its last block lies past the end. The last block
    of the last band is checked, which is the file's last in the usual layouts, COG's included."""
    if dataset.driver != "GTiff":
        return
    rows, columns = dataset.block_shapes[-1]
    last_row = math.ceil(dataset.height / rows) - 1
    last_column = math.ceil(dataset.width / columns) - 1
    tag = f"{last_column}_{last_row}"
    offset = dataset.get_tag_item(f"BLOCK_OFFSET_{tag}", "TIFF", bidx=dataset.count)
    length = dataset.get_tag_item(f"BLOCK_SIZE_{tag}", "TIFF", bidx=dataset.count)
    if not (offset and length and offset.isdigit() and length.isdigit()):
        return  # GDAL doesn't know (an unusual layout): nothing to judge by
    end = int(offset) + int(length)
    if end > size:
        raise Unreadable(f"block {tag} ends at byte {end} of {size}")


def _found(dataset: rasterio.DatasetReader) -> Found:
    properties: dict[str, Json] = {
        "raster:driver": dataset.driver,
        "proj:shape": [dataset.height, dataset.width],
        "raster:bands": [
            {"data_type": dtype, "nodata": dataset.nodata}
            for dtype in list(dataset.dtypes)[:MAX_BANDS]
        ],
        "raster:band_count": dataset.count,
    }
    if dataset.crs is None:
        return Found(properties=properties)
    native = crs.name(dataset.crs)
    properties["proj:transform"] = list[Json](dataset.transform)[:6]  # a-f; g-i are fixed
    bounds = dataset.bounds
    bbox = crs.footprint(native, bounds.left, bounds.bottom, bounds.right, bounds.top)
    return Found(properties=properties, bbox=bbox, crs=native)
