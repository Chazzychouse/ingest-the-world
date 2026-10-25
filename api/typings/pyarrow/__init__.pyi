# Local stubs for what the platform reads with pyarrow. Anything not declared here is Any:
# tests use it to write fixture files.
from collections.abc import Iterator
from typing import Any

class ArrowException(Exception): ...
class DataType: ...

class Field:
    name: str
    type: DataType

class Schema:
    metadata: dict[bytes, bytes] | None
    def __iter__(self) -> Iterator[Field]: ...
    def __len__(self) -> int: ...

def __getattr__(name: str) -> Any: ...
