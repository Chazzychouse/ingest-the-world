"""ESRI Shapefile main files (`.shp`): shape type and extent, from the 100-byte header.

The CRS lives in a `.prj` sidecar, which a single uploaded file doesn't have, so a `.shp` alone
has a native extent but no footprint: guessing its CRS from its numbers could put it anywhere.
https://www.esri.com/content/dam/esrisites/sitecore-archive/Files/Pdfs/library/whitepapers/pdfs/shapefile.pdf
"""

import struct

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Json
from ingest_api.worker.extract import BlobSource, Found, Unreadable

HEADER = 100
FILE_CODE = 9994
VERSION = 1000
CODE = struct.Struct(">i")  # at 0, big-endian
LENGTH = struct.Struct(">i")  # at 24: the file's length in 16-bit words
LAYOUT = struct.Struct("<ii4d")  # at 28: version, shape type, xmin, ymin, xmax, ymax
SHAPE_TYPES = {
    0: "null",
    1: "point",
    3: "polyline",
    5: "polygon",
    8: "multipoint",
    11: "pointz",
    13: "polylinez",
    15: "polygonz",
    18: "multipointz",
    21: "pointm",
    23: "polylinem",
    25: "polygonm",
    28: "multipointm",
    31: "multipatch",
}


def read(source: BlobSource) -> Found:
    head = source.read_exact(0, HEADER)
    (code,) = CODE.unpack_from(head, 0)
    (words,) = LENGTH.unpack_from(head, 24)
    version, shape_type, xmin, ymin, xmax, ymax = LAYOUT.unpack_from(head, 28)
    if code != FILE_CODE or version != VERSION or shape_type not in SHAPE_TYPES:
        raise Unreadable(f"shapefile header: code {code}, version {version}, type {shape_type}")
    if words * 2 != source.size:
        raise Unreadable(f"header says {words * 2} bytes, the file has {source.size}")
    properties: dict[str, Json] = {"shp:shape_type": SHAPE_TYPES[shape_type]}
    if shape_type != 0 and xmin <= xmax and ymin <= ymax:
        properties["proj:bbox"] = [xmin, ymin, xmax, ymax]
    return Found(properties=properties, kind=Kind.VECTOR)
