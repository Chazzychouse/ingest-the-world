"""What an item says about itself, as read from its header (ING-6): STAC-style properties, the
native CRS and a WGS84 footprint, plus how the reading went."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Self

# `extracted`, or why there is nothing: no reader for the format, bytes that don't parse, a
# header that would take more than the byte budget to read, or a reader that crashed, hung or ran
# out of memory (ING-8). Kept in step with the database by tests/test_items.py.
Status = Literal["extracted", "unsupported", "unreadable", "over-budget", "failed"]
type Json = str | int | float | bool | list[Json] | dict[str, Json] | None


@dataclass(frozen=True, slots=True)
class Bbox:
    """A WGS84 footprint, `west, south, east, north` in degrees, as STAC writes it."""

    west: float
    south: float
    east: float
    north: float

    @classmethod
    def checked(cls, west: float, south: float, east: float, north: float) -> Self | None:
        """The box, or None if it isn't a real place: a bad reprojection or a header that
        lies shouldn't put an item somewhere on the map."""
        values = (west, south, east, north)
        if not all(math.isfinite(v) for v in values):
            return None
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            return None
        return cls(west, south, east, north)


@dataclass(frozen=True, slots=True)
class Metadata:
    status: Status
    bytes_read: int  # fetched from storage to learn this; at most the budget (ING-6)
    properties: Mapping[str, Json] = field(default_factory=dict[str, Json])
    bbox: Bbox | None = None
    crs: str | None = None  # "AUTHORITY:code" when the CRS has one, else WKT2
