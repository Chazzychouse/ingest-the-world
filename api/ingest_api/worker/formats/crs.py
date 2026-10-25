"""Coordinate reference systems: naming a file's CRS, and its footprint in WGS84 (via GDAL's
PROJ, through rasterio)."""

from rasterio.crs import CRS
from rasterio.errors import CRSError, RasterioError
from rasterio.warp import transform_bounds

from ingest_api.items.metadata import Bbox

WGS84 = "EPSG:4326"
# Points per edge when reprojecting a box, so curved edges don't clip the footprint.
DENSIFY_POINTS = 21


def name(crs: CRS) -> str:
    """`AUTHORITY:code` (EPSG:32613, OGC:CRS84) when the CRS has one; WKT2 otherwise."""
    code = crs.to_epsg()
    if code is not None:
        return f"EPSG:{code}"
    authority = crs.to_authority()
    if authority is not None:
        return f"{authority[0]}:{authority[1]}"
    return crs.to_wkt(version="WKT2_2019")


def from_wkt(wkt: str) -> str | None:
    try:
        return name(CRS.from_wkt(wkt))
    except CRSError:
        return None


def from_epsg(code: int | None) -> str | None:
    if code is None:
        return None
    try:
        return name(CRS.from_epsg(code))
    except CRSError:
        return None


def from_user_input(value: str) -> str | None:
    """A CRS in any form PROJ reads: a code, WKT, or PROJJSON."""
    try:
        return name(CRS.from_user_input(value))
    except CRSError:
        return None


def footprint(crs: str, west: float, south: float, east: float, north: float) -> Bbox | None:
    """The WGS84 box around these bounds in `crs`, or None if they don't reproject."""
    try:
        box = transform_bounds(crs, WGS84, west, south, east, north, densify_pts=DENSIFY_POINTS)
    except (CRSError, RasterioError):
        return None
    return Bbox.checked(*box)
