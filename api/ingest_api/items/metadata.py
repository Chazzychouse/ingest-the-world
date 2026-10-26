"""What an item says about itself, as read from its header (ING-6): STAC-style properties, the
native CRS, a WGS84 footprint and when it was captured, plus how the reading went."""

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Self

# `extracted`, or why there is nothing: no reader for the format, bytes that don't parse, a
# header that would take more than the byte budget to read, or a reader that crashed, hung or ran
# out of memory (ING-8). Kept in step with the database by tests/test_items.py.
Status = Literal["extracted", "unsupported", "unreadable", "over-budget", "failed"]
type Json = str | int | float | bool | list[Json] | dict[str, Json] | None
# Recorded for every TIFF: whether the original is a COG already. The derive stage makes one only
# when it isn't, and the original's STAC asset says so when it is.
CLOUD_OPTIMIZED = "raster:cloud_optimized"


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


# Capture times outside these years are a header's placeholder or garbage, not a date.
EARLIEST_CAPTURE = dt.datetime(1900, 1, 1, tzinfo=dt.UTC)
LATEST_CAPTURE = dt.datetime(2200, 1, 1, tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class Captured:
    """When the data was captured, in UTC, both ends inclusive; an instant has `start == end`."""

    start: dt.datetime
    end: dt.datetime

    @classmethod
    def checked(cls, start: dt.datetime, end: dt.datetime | None = None) -> Self | None:
        """The range, or None if it isn't a plausible time: headers carry zeroed and made-up
        dates, and those shouldn't put an item in a time filter. Naive times are taken as UTC,
        which is what the formats read here mean when they don't say."""
        first = _utc(start)
        last = first if end is None else _utc(end)
        if not EARLIEST_CAPTURE <= first <= last < LATEST_CAPTURE:
            return None
        return cls(first, last)


def _utc(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC)


@dataclass(frozen=True, slots=True)
class Metadata:
    status: Status
    bytes_read: int  # fetched from storage to learn this; at most the budget (ING-6)
    properties: Mapping[str, Json] = field(default_factory=dict[str, Json])
    bbox: Bbox | None = None
    crs: str | None = None  # "AUTHORITY:code" when the CRS has one, else WKT2
    captured: Captured | None = None
