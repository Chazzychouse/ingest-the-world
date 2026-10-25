from io import RawIOBase
from typing import Any

from pyarrow import Schema

class ParquetSchema:
    def to_arrow_schema(self) -> Schema: ...

class FileMetaData:
    num_rows: int
    schema: ParquetSchema

class ParquetFile:
    metadata: FileMetaData
    def __init__(self, source: str | RawIOBase) -> None: ...

def __getattr__(name: str) -> Any: ...
