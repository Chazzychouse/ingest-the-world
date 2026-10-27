"""Textures for tiles: each tile gets its own image, cropped to the part of the source texture its
triangles use and scaled to its level, so a viewer never holds a model's whole texture per tile.

A source image (a JPEG or PNG from the model) is decoded once, by GDAL, into a tiled TIFF with
overviews in scratch; a tile's crop then reads only the blocks it needs, from the overview that
suits its size. Texture coordinates outside [0, 1] mean the texture repeats, which a crop can't
express, so such a part takes the whole image, scaled. Crops are JPEG, or PNG where the source
has transparency. Deterministic for a given GDAL.
"""

import math
import warnings
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio._err import CPLE_BaseError
from rasterio.enums import Resampling
from rasterio.errors import NotGeoreferencedWarning, RasterioError
from rasterio.io import MemoryFile
from rasterio.windows import Window

from ingest_api.worker.meshes.model import Floats
from ingest_api.worker.recipe import Unrenderable

JPEG_QUALITY = 85
BLOCK_PX = 512
MAX_SOURCE_PX = 32_768  # per side; a bigger texture is refused rather than decoded
PAD_PX = 2  # around a crop, so filtering at its edge samples the right colours
OVERVIEW_FACTORS = (2, 4, 8, 16, 32)


@dataclass(frozen=True, slots=True)
class Baked:
    """A tile's own texture, and its part's texture coordinates remapped onto it."""

    image: bytes
    media_type: str
    uvs: Floats


class Textures:
    """The source images a model's materials use, prepared once each, in `scratch`."""

    def __init__(self, image: Callable[[int], bytes | None], scratch: Path):
        self._image, self._scratch = image, scratch
        self._prepared: dict[int, Path | None] = {}

    def bake(self, image: int, uvs: Floats, *, texels: int) -> Baked | None:
        """Image `image` cropped to `uvs` and scaled to at most `texels` on its longer side, or
        None if the model doesn't hold that image as a JPEG or PNG."""
        source = self._source(image)
        if source is None or len(uvs) == 0:
            return None
        with _gdal(), rasterio.open(str(source)) as dataset:
            width, height = dataset.width, dataset.height
            window, low, span = _window(uvs, width, height)
            pixels = _read(dataset, window, texels)
            alpha = dataset.count == 4
        remapped = np.empty_like(uvs)
        remapped[:, 0] = (uvs[:, 0] * width - low[0]) / span[0]
        remapped[:, 1] = (uvs[:, 1] * height - low[1]) / span[1]
        media = "image/png" if alpha else "image/jpeg"
        return Baked(_encode(pixels, png=alpha), media, remapped.astype(np.float32))

    def _source(self, image: int) -> Path | None:
        if image not in self._prepared:
            self._prepared[image] = self._prepare(image)
        return self._prepared[image]

    def _prepare(self, image: int) -> Path | None:
        encoded = self._image(image)
        if encoded is None:
            return None
        out = self._scratch / f"texture-{image}.tif"
        with _gdal(), MemoryFile(encoded) as memory, memory.open() as dataset:
            if max(dataset.width, dataset.height) > MAX_SOURCE_PX:
                raise Unrenderable(f"a {dataset.width} x {dataset.height} texture")
            bands = (
                [1, 2, 3, 4]
                if dataset.count >= 4
                else [1, 2, 3]
                if dataset.count >= 3
                else [1, 1, 1]
            )
            with rasterio.open(
                str(out),
                "w",
                driver="GTiff",
                width=dataset.width,
                height=dataset.height,
                count=len(bands),
                dtype="uint8",
                tiled=True,
                blockxsize=BLOCK_PX,
                blockysize=BLOCK_PX,
            ) as tiff:
                for _, window in tiff.block_windows(1):  # bounded: one block at a time
                    tiff.write(dataset.read(bands, window=window).astype(np.uint8), window=window)
                factors = [
                    f for f in OVERVIEW_FACTORS if max(dataset.width, dataset.height) // f >= 64
                ]
                tiff.build_overviews(factors, Resampling.average)
        return out


def _window(
    uvs: Floats, width: int, height: int
) -> tuple[Window, tuple[int, int], tuple[int, int]]:
    """The pixel window `uvs` use, padded; and its corner and size, for remapping."""
    low, high = uvs.min(axis=0).astype(np.float64), uvs.max(axis=0).astype(np.float64)
    if float(low.min()) < 0 or float(high.max()) > 1:  # repeating: the whole image
        low, high = np.zeros(2), np.ones(2)
    x0 = max(0, math.floor(low[0] * width) - PAD_PX)
    y0 = max(0, math.floor(low[1] * height) - PAD_PX)
    x1 = min(width, math.ceil(high[0] * width) + PAD_PX)
    y1 = min(height, math.ceil(high[1] * height) + PAD_PX)
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    return Window(x0, y0, w, h), (x0, y0), (w, h)


def _read(dataset: rasterio.DatasetReader, window: Window, texels: int) -> np.ndarray:
    w, h = int(window.width), int(window.height)
    scale = min(1.0, texels / max(w, h))
    shape = (dataset.count, max(1, round(h * scale)), max(1, round(w * scale)))
    return dataset.read(window=window, out_shape=shape, resampling=Resampling.average)


def _encode(pixels: np.ndarray, *, png: bool) -> bytes:
    count, height, width = pixels.shape
    options: dict[str, object] = {} if png else {"QUALITY": JPEG_QUALITY}
    driver = "PNG" if png else "JPEG"
    with _gdal(), MemoryFile() as memory:
        with memory.open(
            driver=driver, width=width, height=height, count=count, dtype="uint8", **options
        ) as out:
            out.write(pixels)
        return memory.read()


@contextmanager
def _gdal() -> Generator[None]:
    """GDAL quiet about pixels without a place, and its failures as Unrenderable."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            yield
    except (RasterioError, CPLE_BaseError) as exc:
        raise Unrenderable(f"a texture: {exc}") from exc
