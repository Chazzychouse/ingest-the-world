"""Where rasterio's dataset classes live; the package re-exports them. rio-tiler names this one."""

from typing import Any

from rasterio import DatasetReader as DatasetReader

def __getattr__(name: str) -> Any: ...
