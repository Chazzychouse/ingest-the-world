from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal

from pyarrow import RecordBatchReader

def open_arrow(
    path_or_buffer: str | Path,
    /,
    *,
    batch_size: int = 65536,
    use_pyarrow: Literal[True],
) -> AbstractContextManager[tuple[dict[str, Any], RecordBatchReader]]: ...
def __getattr__(name: str) -> Any: ...
