# Local stubs for what the platform reads with rasterio, which ships no type information.
# Anything not declared here is Any: tests use it to write fixture files.
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Self

from rasterio.abc import FileContainer
from rasterio.coords import BoundingBox
from rasterio.crs import CRS

__version__: str
__gdal_version__: str

class Affine:
    def __iter__(self) -> Iterator[float]: ...

class DatasetReader:
    driver: str
    width: int
    height: int
    count: int
    dtypes: tuple[str, ...]
    nodata: float | None
    crs: CRS | None
    bounds: BoundingBox
    transform: Affine
    def __getattr__(self, name: str) -> Any: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

def open(
    fp: str, mode: str = "r", *, opener: FileContainer | None = None, **profile: Any
) -> DatasetReader: ...
def __getattr__(name: str) -> Any: ...
