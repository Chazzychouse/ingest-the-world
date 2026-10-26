"""A preview's COG, read and drawn: where it lies and how it stretches, for its TileJSON, and its
tiles as PNGs.

The COG is opened the way the worker opens a raster's header (`open_blob`): GDAL sees one blob,
through a `Budget` that caps what one request fetches from storage, so no tile costs more than
MAX_REQUEST_BYTES however the file is laid out. Tiles are drawn as `ingest_api.display` draws
every raster, with nodata transparent; a preview's stretch is taken once, from the whole raster's
overview, so its tiles meet without seams.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial

import numpy as np
import rasterio
from numpy.typing import NDArray
from rasterio.errors import RasterioError
from rio_tiler.constants import WGS84_CRS
from rio_tiler.errors import RioTilerError, TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.models import ImageData
from rio_tiler.utils import render

from ingest_api import display
from ingest_api.items.store import Tile
from ingest_api.ports import BlobNotFound, BlobStore, StorageError
from ingest_api.previews import Preview
from ingest_api.worker.extract import BlobSource, Budget
from ingest_api.worker.formats.raster import open_blob

# The most one request fetches from storage: a few of a COG's 512 px blocks, in every band.
MAX_REQUEST_BYTES = 32 * 1024 * 1024
# GDAL's block cache, in MiB, per process.
GDAL_CACHE_MIB = 128
TILE_PX = 256
# The stretch is taken from an overview at most this many pixels on its longer side.
STRETCH_SAMPLE_PX = 512


class Undrawable(Exception):
    """The COG can't be drawn within the budget: cut short, unreadable, or too costly to read
    at this zoom."""


@dataclass(frozen=True, slots=True)
class Extent:
    bounds: tuple[float, float, float, float]  # west, south, east, north, in WGS 84
    minzoom: int
    maxzoom: int
    stretch: display.Stretch | None  # for samples beyond 8 bits; None if there is nothing to draw


@dataclass(frozen=True, slots=True)
class _Opened:
    dataset: rasterio.DatasetReader
    reader: Reader  # over `dataset`, in web mercator tiles


@contextmanager
def _opened(blobs: BlobStore, preview: Preview) -> Generator[_Opened]:
    """The preview's COG, read within one request's budget. Raises BlobNotFound if it's gone,
    StorageError if storage failed under it, and Undrawable for anything else."""
    props = blobs.head(preview.container, preview.blob)
    if props is None:
        raise BlobNotFound(preview.blob)
    read = partial(blobs.read_range, preview.container, preview.blob)
    budget = Budget(read, preview.blob, props.size, limit=MAX_REQUEST_BYTES)
    try:
        with (
            rasterio.Env(GDAL_CACHEMAX=GDAL_CACHE_MIB),
            open_blob(BlobSource(budget.fetch, preview.blob, props.size)) as dataset,
            Reader(input=None, dataset=dataset, options={}) as reader,
        ):
            yield _Opened(dataset, reader)
    except (RasterioError, RioTilerError) as exc:
        # A failed fetch looks like the end of the file to GDAL; the budget knows the cause.
        if isinstance(budget.failure, StorageError):
            raise budget.failure from exc
        if isinstance(exc, TileOutsideBounds):
            raise
        raise Undrawable(str(budget.failure or exc)) from exc
    if budget.failure is not None:  # GDAL carried on past a failed read
        raise Undrawable(str(budget.failure))


def _indexes(dataset: rasterio.DatasetReader) -> list[int]:
    if dataset.dtypes[0].startswith("complex"):
        raise Undrawable("complex samples have no picture")
    if not display.is_palette(dataset.colorinterp):
        return display.bands(dataset.colorinterp)
    if dataset.dtypes[0] not in display.PALETTE_TYPES:
        raise Undrawable(f"a palette over {dataset.dtypes[0]} samples")
    return [1]


def _valid(image: ImageData) -> NDArray[np.bool_]:
    """Pixels with a finite sample in every band, none of them nodata or outside the raster."""
    masked = np.ma.getmaskarray(image.array)
    return ~np.any(masked, axis=0) & np.all(np.isfinite(image.data), axis=0)


def extent(blobs: BlobStore, preview: Preview) -> Extent:
    with _opened(blobs, preview) as opened:
        dataset, reader = opened.dataset, opened.reader
        indexes = _indexes(dataset)
        west, south, east, north = reader.get_geographic_bounds(WGS84_CRS)
        stretch = None
        if dataset.dtypes[0] != "uint8" and not display.is_palette(dataset.colorinterp):
            sample = reader.preview(indexes=indexes, max_size=STRETCH_SAMPLE_PX)
            stretch = display.stretch(sample.data, _valid(sample))
        return Extent((west, south, east, north), reader.minzoom, reader.maxzoom, stretch)


def tile(blobs: BlobStore, preview: Preview, at: Tile, stretch: display.Stretch | None) -> bytes:
    """The tile `at` as a PNG, its samples stretched over `stretch` (or the tile's own, if
    none). Raises TileOutsideBounds for a tile the raster doesn't reach."""
    with _opened(blobs, preview) as opened:
        dataset = opened.dataset
        indexes = _indexes(dataset)
        image = opened.reader.tile(at.x, at.y, at.z, tilesize=TILE_PX, indexes=indexes)
        valid = _valid(image)
        if display.is_palette(dataset.colorinterp):
            colours = display.palette(image.data[0], dataset.colormap(1))
        else:
            colours = display.eight_bit(image.data, valid, stretch)
    alpha = np.where(valid, display.OPAQUE, 0).astype(np.uint8)
    return render(colours, mask=alpha, img_format="PNG")
