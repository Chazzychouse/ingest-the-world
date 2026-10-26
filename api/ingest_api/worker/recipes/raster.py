"""Raster derivatives, rendered by GDAL (through rasterio) from the original's local copy.

- `cog`: a Cloud-Optimized GeoTIFF the tiler can read a tile at a time (512 px internal tiles,
  overviews, deflate). Only for georeferenced rasters, since a tile needs a place, and not for
  originals the raster reader found cloud-optimized already: the original serves.
- `thumbnail`: a PNG at most 512 px on its longer side, read through the overviews where there
  are any. Red, green and blue bands are shown as such, a palette through its colours, anything
  else as its first band in grey; bands beyond 8 bits are stretched between their 2nd and 98th
  percentiles, and nodata is transparent.

Each opens only its original, with sidecar files ignored and none written, and keeps GDAL's
temporary files in its scratch directory. Both are deterministic for a given GDAL (ING-7); a
change here that would change their output raises their version.
"""

import warnings
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import rasterio.shutil
from numpy.typing import NDArray
from rasterio._err import CPLE_BaseError
from rasterio.enums import ColorInterp, Resampling
from rasterio.errors import NotGeoreferencedWarning, RasterioError

from ingest_api.items.kinds import Kind
from ingest_api.worker.formats.raster import CLOUD_OPTIMIZED
from ingest_api.worker.recipe import Source, Unrenderable

SOFTWARE: Mapping[str, str] = {"GDAL": rasterio.__gdal_version__, "rasterio": rasterio.__version__}
# GDAL's block cache, in MiB: well inside the render sandbox's memory limit.
GDAL_CACHE_MIB = 256
COG_BLOCK_PX = 512
THUMBNAIL_PX = 512
STRETCH_PERCENTILES = (2.0, 98.0)
RGB = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
OPAQUE = 255


@contextmanager
def _gdal(scratch: Path) -> Generator[None]:
    """GDAL configured for one render, its failures as Unrenderable."""
    options: dict[str, str | int] = {
        "GDAL_CACHEMAX": GDAL_CACHE_MIB,
        "CPL_TMPDIR": str(scratch),
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",  # no sidecars: only the original is read
        "GDAL_PAM_ENABLED": "NO",  # and no `.aux.xml` written beside it
    }
    try:
        with rasterio.Env(**options), warnings.catch_warnings():
            # An image without georeferencing is normal (a PNG): its thumbnail is still useful.
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            yield
    # GDAL's own errors surface as CPLE_*, from a driver mid-copy as much as on open.
    except (RasterioError, CPLE_BaseError) as exc:
        raise Unrenderable(str(exc)) from exc


class Cog:
    role, version, media_type, suffix = (
        "cog",
        1,
        "image/tiff; application=geotiff; profile=cloud-optimized",
        ".tif",
    )
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        return (
            _readable_raster(source)
            and source.crs is not None
            and source.properties.get(CLOUD_OPTIMIZED) is not True
        )

    def render(self, original: Path, scratch: Path) -> Path:
        out = scratch / "cog.tif"
        with _gdal(scratch):
            rasterio.shutil.copy(
                str(original),
                str(out),
                driver="COG",
                COMPRESS="DEFLATE",
                PREDICTOR="YES",
                BLOCKSIZE=COG_BLOCK_PX,
                BIGTIFF="IF_SAFER",
            )
        return out


class Thumbnail:
    role, version, media_type, suffix = "thumbnail", 1, "image/png", ".png"
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        return _readable_raster(source)

    def render(self, original: Path, scratch: Path) -> Path:
        out = scratch / "thumbnail.png"
        with _gdal(scratch):
            with rasterio.open(str(original)) as dataset:
                pixels = _pixels(dataset)
            count, height, width = pixels.shape
            profile: dict[str, Any] = {"driver": "PNG", "width": width, "height": height}
            with rasterio.open(str(out), "w", count=count, dtype="uint8", **profile) as png:
                png.write(pixels)
        return out


def _readable_raster(source: Source) -> bool:
    """A raster whose header read cleanly. One that didn't may render all the same, from what
    survives (a truncated COG's overviews), but would show a broken file as a whole one."""
    return source.kind == Kind.RASTER and source.extraction == "extracted"


def _fit(height: int, width: int) -> tuple[int, int]:
    scale = min(1.0, THUMBNAIL_PX / max(height, width))
    return max(1, round(height * scale)), max(1, round(width * scale))


def _pixels(dataset: rasterio.DatasetReader) -> NDArray[np.uint8]:
    """The thumbnail's bands, 8-bit, with an alpha band if any of it is nodata or NaN."""
    if dataset.dtypes[0].startswith("complex"):
        raise Unrenderable("complex samples have no picture")
    height, width = _fit(dataset.height, dataset.width)
    mask = dataset.dataset_mask(out_shape=(height, width), resampling=Resampling.nearest)
    valid = mask == OPAQUE
    if dataset.colorinterp[0] == ColorInterp.palette:
        colours = _palette(dataset, height, width)
    else:
        bands = [1, 2, 3] if tuple(dataset.colorinterp[:3]) == RGB else [1]
        shape = (len(bands), height, width)
        data = dataset.read(bands, out_shape=shape, resampling=Resampling.average)
        valid &= np.all(np.isfinite(data), axis=0)
        colours = _eight_bit(data, valid)
    if bool(np.all(valid)):
        return colours
    alpha = np.where(valid, OPAQUE, 0).astype(np.uint8)
    return np.concatenate([colours, alpha[np.newaxis]])


def _palette(dataset: rasterio.DatasetReader, height: int, width: int) -> NDArray[np.uint8]:
    """A paletted band through its colours, as red, green and blue."""
    if dataset.dtypes[0] not in ("uint8", "uint16"):
        raise Unrenderable(f"a palette over {dataset.dtypes[0]} samples")
    shape = (1, height, width)
    indexes = dataset.read([1], out_shape=shape, resampling=Resampling.nearest)[0]
    table = np.zeros((2 ** (8 * indexes.itemsize), 3), dtype=np.uint8)
    for index, (red, green, blue, _) in dataset.colormap(1).items():
        table[index] = (red, green, blue)
    return np.moveaxis(table[indexes], -1, 0)


def _eight_bit(data: NDArray[Any], valid: NDArray[np.bool_]) -> NDArray[np.uint8]:
    """`data` as it is if it is 8-bit; otherwise stretched to 0-255 between the percentiles of
    its valid samples, all bands together so colours keep their balance."""
    if data.dtype == np.uint8:
        return data
    if not bool(np.any(valid)):
        return np.zeros(data.shape, dtype=np.uint8)
    values = data.astype(np.float64)
    low, high = (float(p) for p in np.percentile(values[:, valid], STRETCH_PERCENTILES))
    span = high - low if high > low else 1.0
    scaled = np.nan_to_num((values - low) / span * OPAQUE, nan=0.0)
    return np.clip(scaled, 0, OPAQUE).astype(np.uint8)
