# Local stubs for what the platform reads and renders with rasterio, which ships no type
# information. Anything not declared here is Any: tests use it to write fixture files.
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Self

import numpy as np
from numpy.typing import NDArray
from rasterio.abc import FileContainer
from rasterio.coords import BoundingBox
from rasterio.crs import CRS
from rasterio.enums import ColorInterp, Resampling
from rasterio.windows import Window

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
    block_shapes: list[tuple[int, int]]
    colorinterp: tuple[ColorInterp, ...]
    def overviews(self, bidx: int) -> list[int]: ...
    def colormap(self, bidx: int) -> dict[int, tuple[int, int, int, int]]: ...
    def read(
        self,
        indexes: list[int] | None = None,
        *,
        window: Window | None = None,
        out_shape: tuple[int, int, int] | None = None,
        resampling: Resampling = ...,
    ) -> NDArray[Any]: ...
    def block_windows(self, bidx: int = 0) -> Iterator[tuple[tuple[int, int], Window]]: ...
    def build_overviews(self, factors: list[int], resampling: Resampling = ...) -> None: ...
    def dataset_mask(
        self, *, out_shape: tuple[int, int] | None = None, resampling: Resampling = ...
    ) -> NDArray[np.uint8]: ...
    def write(self, arr: NDArray[Any], *, window: Window | None = None) -> None: ...
    def get_tag_item(
        self, ns: str, dm: str | None = None, bidx: int = 0, ovr: int | None = None
    ) -> str | None: ...
    def tags(self, bidx: int = 0, ns: str | None = None) -> dict[str, str]: ...
    def tag_namespaces(self, bidx: int = 0) -> list[str]: ...
    def __getattr__(self, name: str) -> Any: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

class Env:
    def __init__(self, **options: str | int) -> None: ...
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
