"""Rasters (GeoTIFF and COG, PNG, JPEG and anything else GDAL reads): size, bands, CRS and
footprint, from the header alone.

GDAL reads through rasterio's Python-filesystem plugin, so every byte it asks for comes through
`BlobSource` and counts against the budget; it never gets a URL or a credential of its own.
Opening a dataset reads its header and IFDs; no pixel data is touched.
"""

from typing import override

import rasterio
from rasterio.abc import FileContainer
from rasterio.errors import RasterioError

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
        with rasterio.open(PATH, opener=_OneBlob(source)) as dataset:
            return _found(dataset)
    except RasterioError as exc:
        raise Unreadable(str(exc)) from exc


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
