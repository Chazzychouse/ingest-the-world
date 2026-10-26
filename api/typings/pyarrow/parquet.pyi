from io import RawIOBase
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pyarrow import RecordBatch, Schema

class ParquetSchema:
    def to_arrow_schema(self) -> Schema: ...

class FileMetaData:
    num_rows: int
    schema: ParquetSchema

class ParquetFile:
    metadata: FileMetaData
    def __init__(self, source: str | RawIOBase) -> None: ...

class ParquetWriter:
    schema: Schema
    def __init__(self, where: str | Path, schema: Schema, *, compression: str) -> None: ...
    def write_batch(self, batch: RecordBatch) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None: ...

def __getattr__(name: str) -> Any: ...
