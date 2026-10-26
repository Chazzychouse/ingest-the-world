"""How a raster is drawn as a picture: by its thumbnail (the derive stage's `thumbnail` recipe)
and by its preview tiles (the tiler), so the two look alike.

Red, green and blue bands are shown as such, a palette through its colours, anything else as its
first band in grey. Samples beyond 8 bits are stretched to 0-255 between the 2nd and 98th
percentiles of the valid samples, all bands together so colours keep their balance. A thumbnail
is one picture and takes its own percentiles; a preview's tiles share the percentiles of the
whole raster's overview, so they meet without seams.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray
from rasterio.enums import ColorInterp

RGB = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
OPAQUE = 255
STRETCH_PERCENTILES = (2.0, 98.0)
# A palette indexes its colours with these; wider samples are no palette GDAL writes.
PALETTE_TYPES = ("uint8", "uint16")

type Stretch = tuple[float, float]  # the samples drawn as 0 and as 255


def is_palette(colorinterp: Sequence[ColorInterp]) -> bool:
    return colorinterp[0] == ColorInterp.palette


def palette(
    indexes: NDArray[Any], colormap: Mapping[int, tuple[int, int, int, int]]
) -> NDArray[np.uint8]:
    """A paletted band's `indexes` (rows and columns, of a `PALETTE_TYPES` type) through its
    colours, as red, green and blue bands."""
    table = np.zeros((2 ** (8 * indexes.itemsize), 3), dtype=np.uint8)
    for index, (red, green, blue, _) in colormap.items():
        table[index] = (red, green, blue)
    return np.moveaxis(table[indexes], -1, 0)


def bands(colorinterp: Sequence[ColorInterp]) -> list[int]:
    """The bands drawn, 1-based: red, green and blue, or else the first."""
    return [1, 2, 3] if tuple(colorinterp[:3]) == RGB else [1]


def stretch(data: NDArray[Any], valid: NDArray[np.bool_]) -> Stretch | None:
    """The percentiles of `data`'s valid samples (bands first, then rows and columns); None if
    no sample is valid."""
    if not bool(np.any(valid)):
        return None
    values = data.astype(np.float64)
    low, high = (float(p) for p in np.percentile(values[:, valid], STRETCH_PERCENTILES))
    return low, high


def eight_bit(
    data: NDArray[Any], valid: NDArray[np.bool_], span: Stretch | None = None
) -> NDArray[np.uint8]:
    """`data` as it is if it is 8-bit; otherwise stretched to 0-255 over `span`, or over its own
    percentiles if no span is given."""
    if data.dtype == np.uint8:
        return data
    span = span or stretch(data, valid)
    if span is None:
        return np.zeros(data.shape, dtype=np.uint8)
    low, high = span
    width = high - low if high > low else 1.0
    scaled = np.nan_to_num((data.astype(np.float64) - low) / width * OPAQUE, nan=0.0)
    return np.clip(scaled, 0, OPAQUE).astype(np.uint8)
