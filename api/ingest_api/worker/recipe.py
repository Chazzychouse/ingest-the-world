"""What a recipe is: one way of making one kind of derivative (its role) from an item's original.

The derive stage (`derive.py`) decides when a recipe runs and keeps its output; a recipe only
says whether it applies to an original and renders one file from it into local scratch.
Recipes live in `recipes/`, one per (kind, role).
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from ingest_api.items.kinds import Kind
from ingest_api.ports import StorageError

# Read size when a recipe streams its original; memory per render stays at one chunk.
CHUNK = 8 * 1024 * 1024

type Fetch = Callable[[int, int], bytes]  # (offset, length) -> bytes of the original


class Unrenderable(Exception):
    """This recipe can't make its derivative from this original; trying again won't help."""


@dataclass(frozen=True, slots=True)
class Source:
    """An item's original in `raw/`, read through the blob store."""

    item_id: UUID
    collection_id: str
    kind: Kind
    media_type: str
    size: int
    sha256: str
    fetch: Fetch

    def chunks(self) -> Iterator[bytes]:
        """The whole original, one chunk at a time."""
        for offset in range(0, self.size, CHUNK):
            expected = min(CHUNK, self.size - offset)
            chunk = self.fetch(offset, expected)
            if len(chunk) != expected:
                raise StorageError(f"the original of {self.item_id} is short of {self.size} bytes")
            yield chunk


class Recipe(Protocol):
    @property
    def role(self) -> str:
        """What the derivative is for: `cog`, `thumbnail`. One asset per role and version."""
        ...

    @property
    def version(self) -> int:
        """Raised whenever the output would change, so old and new derivatives coexist."""
        ...

    @property
    def media_type(self) -> str: ...

    @property
    def suffix(self) -> str:
        """The derivative blob's file extension, with its dot."""
        ...

    @property
    def deterministic(self) -> bool:
        """Whether the same original always renders to the same bytes (ING-7)."""
        ...

    @property
    def software(self) -> Mapping[str, str]:
        """What renders it, by name and version: STAC `processing:software`."""
        ...

    def applies(self, source: Source) -> bool: ...

    def render(self, source: Source, scratch: Path) -> Path:
        """Writes the derivative into `scratch`, an empty directory of its own, and returns its
        path. Raises Unrenderable when the original can't make one."""
        ...
