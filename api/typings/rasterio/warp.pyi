from rasterio.crs import CRS

def transform_bounds(
    src_crs: str | CRS,
    dst_crs: str | CRS,
    left: float,
    bottom: float,
    right: float,
    top: float,
    densify_pts: int = 21,
) -> tuple[float, float, float, float]: ...
