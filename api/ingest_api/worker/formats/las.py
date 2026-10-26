"""LAS, LAZ and COPC point clouds: the public header block and its variable-length records.

The header (at most 375 bytes) holds the point count and the bounds; the VLRs after it hold the
CRS (as OGC WKT, or as GeoTIFF keys in older files) and mark COPC files. Point records start at
`offset to point data` and are never read. LAZ compresses only the points, so its header is
the same. https://www.asprs.org/wp-content/uploads/2019/07/LAS_1_4_r15.pdf

The header's file creation day (day of year, year; GMT) is the nearest it has to a capture time,
so an item is captured sometime that day.
"""

import datetime as dt
import struct

from ingest_api.items.metadata import Captured, Json
from ingest_api.worker.extract import BlobSource, Found, Unreadable
from ingest_api.worker.formats import crs

SIGNATURE = b"LASF"
# The fields used, by offset into the public header block (LAS 1.0 to 1.4).
VERSION = struct.Struct("<BB")  # at 24
CREATED = struct.Struct("<HH")  # at 90: file creation day of year (1-366), year
LAYOUT = struct.Struct("<HII")  # at 94: header size, offset to point data, number of VLRs
POINT_FORMAT = struct.Struct("<BHI")  # at 104: format, record length, legacy point count
BOUNDS = struct.Struct("<6d")  # at 179: max x, min x, max y, min y, max z, min z
POINT_COUNT_14 = struct.Struct("<Q")  # at 247, LAS 1.4 only
VLR_HEADER = struct.Struct("<H16sHH32s")  # reserved, user id, record id, length, description
MIN_HEADER = 227
LAS_14_HEADER = 375
MAX_VLRS = 64
COMPRESSED = 0xC0  # LAZ sets one of the top two bits of the point format
# Record ids under the "LASF_Projection" user id.
OGC_WKT, GEOKEY_DIRECTORY = 2112, 34735
# GeoTIFF keys naming a CRS by EPSG code: projected, then geographic.
CRS_KEYS = (3072, 2048)


def read(source: BlobSource) -> Found:
    head = source.read_exact(0, MIN_HEADER)
    if not head.startswith(SIGNATURE):
        raise Unreadable("no LASF signature")
    major, minor = VERSION.unpack_from(head, 24)
    header_size, point_offset, vlr_count = LAYOUT.unpack_from(head, 94)
    point_format, _, legacy_count = POINT_FORMAT.unpack_from(head, 104)
    max_x, min_x, max_y, min_y, max_z, min_z = BOUNDS.unpack_from(head, 179)
    day, year = CREATED.unpack_from(head, 90)
    if header_size < MIN_HEADER or point_offset < header_size:
        raise Unreadable(f"header size {header_size}, points at {point_offset}")
    count = legacy_count
    if (major, minor) >= (1, 4) and header_size >= LAS_14_HEADER:
        (count,) = POINT_COUNT_14.unpack(source.read_exact(247, POINT_COUNT_14.size))
    records = _vlrs(source, header_size, point_offset, vlr_count)
    native = _crs(records)
    properties: dict[str, Json] = {
        "pc:count": count,
        "pc:type": "lidar",
        "pc:encoding": _encoding(point_format, records),
        "las:version": f"{major}.{minor}",
        "las:point_format": point_format & ~COMPRESSED,
        "pc:extent": {"min": [min_x, min_y, min_z], "max": [max_x, max_y, max_z]},
    }
    bbox = crs.footprint(native, min_x, min_y, max_x, max_y) if native else None
    return Found(properties=properties, bbox=bbox, crs=native, captured=_captured(day, year))


def _captured(day: int, year: int) -> Captured | None:
    """The creation day as a range; writers leave zeros when they don't know."""
    if not (1 <= day <= 366 and year >= 1):
        return None
    start = dt.datetime(year, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=day - 1)
    if start.year != year:
        return None  # day 366 of a common year
    return Captured.checked(start, start + dt.timedelta(days=1, microseconds=-1))


def _vlrs(source: BlobSource, start: int, end: int, count: int) -> dict[tuple[str, int], bytes]:
    """The VLRs between the header and the points, by (user id, record id)."""
    records: dict[tuple[str, int], bytes] = {}
    offset = start
    for _ in range(min(count, MAX_VLRS)):
        if offset + VLR_HEADER.size > end:
            raise Unreadable("VLRs overrun the point data")
        _, user, record, length, _ = VLR_HEADER.unpack(source.read_exact(offset, VLR_HEADER.size))
        offset += VLR_HEADER.size
        if offset + length > end:
            raise Unreadable("a VLR overruns the point data")
        key = (user.rstrip(b"\x00").decode("ascii", "replace"), record)
        records.setdefault(key, source.read_exact(offset, length))
        offset += length
    return records


def _encoding(point_format: int, records: dict[tuple[str, int], bytes]) -> str:
    if ("copc", 1) in records:
        return "copc"
    return "laszip" if point_format & COMPRESSED else "las"


def _crs(records: dict[tuple[str, int], bytes]) -> str | None:
    wkt = records.get(("LASF_Projection", OGC_WKT))
    if wkt:
        return crs.from_wkt(wkt.rstrip(b"\x00").decode("utf-8", "replace"))
    keys = records.get(("LASF_Projection", GEOKEY_DIRECTORY))
    return crs.from_epsg(_epsg(keys)) if keys else None


def _epsg(directory: bytes) -> int | None:
    """The EPSG code a GeoKeyDirectory names, if it names one directly."""
    shorts = struct.unpack(f"<{len(directory) // 2}H", directory[: len(directory) // 2 * 2])
    if len(shorts) < 4:
        return None
    entries = {
        shorts[i]: shorts[i + 3]
        for i in range(4, min(len(shorts) - 3, 4 + 4 * shorts[3]), 4)
        if shorts[i + 1] == 0  # stored inline, not in another tag
    }
    return next((entries[k] for k in CRS_KEYS if entries.get(k, 0) not in (0, 32767)), None)
