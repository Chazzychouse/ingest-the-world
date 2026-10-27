"""Where rasterio's dataset classes live; the package re-exports them. rio-tiler names this one."""

from types import TracebackType
from typing import Any, Self

from rasterio import DatasetReader as DatasetReader

class MemoryFile:
    def __init__(self, file_or_bytes: bytes | None = None) -> None: ...
    def open(self, **profile: Any) -> DatasetReader: ...
    def read(self) -> bytes: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

def __getattr__(name: str) -> Any: ...
