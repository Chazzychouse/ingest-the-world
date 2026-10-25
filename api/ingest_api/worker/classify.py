"""First-pass classification: what kind of file this is, from its first bytes and its name.

Magic bytes win over the extension, since names lie more often than headers. Formats with no
signature (text formats, OBJ) fall back to the extension. Anything else is `generic`, which
the pipeline still catalogs. Refining ZIP bundles by their central directory comes with
header extraction (ING-6).
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from ingest_api.items.kinds import Kind

# How much of the file classification looks at; the pipeline reads no more for it.
HEAD_BYTES = 4096


class Media(StrEnum):
    """The media types the classifier recognises (`worker.formats` reads some of them)."""

    TIFF = "image/tiff"
    PNG = "image/png"
    JPEG = "image/jpeg"
    GLB = "model/gltf-binary"
    GLTF = "model/gltf+json"
    PLY = "model/ply"
    OBJ = "model/obj"
    E57 = "model/e57"
    LAS = "application/vnd.las"
    LAZ = "application/vnd.laszip"
    PARQUET = "application/vnd.apache.parquet"
    GEOPACKAGE = "application/geopackage+sqlite3"
    SHAPEFILE = "application/vnd.shp"
    GEOJSON = "application/geo+json"
    CSV = "text/csv"
    JSONL = "application/jsonl"
    PDF = "application/pdf"
    TEXT = "text/plain"
    OCTET_STREAM = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class Classification:
    kind: Kind
    media_type: str


_SIGNATURES: tuple[tuple[bytes, Classification], ...] = (
    (b"II*\x00", Classification(Kind.RASTER, Media.TIFF)),
    (b"MM\x00*", Classification(Kind.RASTER, Media.TIFF)),
    (b"II+\x00", Classification(Kind.RASTER, Media.TIFF)),  # BigTIFF
    (b"\x89PNG\r\n\x1a\n", Classification(Kind.RASTER, Media.PNG)),
    (b"\xff\xd8\xff", Classification(Kind.RASTER, Media.JPEG)),
    (b"glTF", Classification(Kind.MESH, Media.GLB)),
    (b"ply\n", Classification(Kind.MESH, Media.PLY)),
    (b"ASTM-E57", Classification(Kind.POINTCLOUD, Media.E57)),
    (b"PAR1", Classification(Kind.TABULAR, Media.PARQUET)),
    (b"SQLite format 3\x00", Classification(Kind.VECTOR, Media.GEOPACKAGE)),
    (b"\x00\x00\x27\x0a", Classification(Kind.VECTOR, Media.SHAPEFILE)),
    (b"%PDF-", Classification(Kind.DOCUMENT, Media.PDF)),
)

# LAS and LAZ share the `LASF` signature; the extension says whether it's compressed.
_LAS = b"LASF"
_LAZ = Classification(Kind.POINTCLOUD, Media.LAZ)
_LAS_PLAIN = Classification(Kind.POINTCLOUD, Media.LAS)

_EXTENSIONS: dict[str, Classification] = {
    ".geojson": Classification(Kind.VECTOR, Media.GEOJSON),
    ".gltf": Classification(Kind.MESH, Media.GLTF),
    ".obj": Classification(Kind.MESH, Media.OBJ),
    ".csv": Classification(Kind.TABULAR, Media.CSV),
    ".jsonl": Classification(Kind.TABULAR, Media.JSONL),
    ".txt": Classification(Kind.DOCUMENT, Media.TEXT),
}


def classify(head: bytes, filename: str, declared_type: str | None = None) -> Classification:
    """`head` is the start of the file (at most HEAD_BYTES are looked at)."""
    head = head[:HEAD_BYTES]
    extension = PurePosixPath(filename).suffix.lower()
    if head.startswith(_LAS):
        return _LAZ if extension == ".laz" else _LAS_PLAIN
    for signature, classification in _SIGNATURES:
        if head.startswith(signature):
            return classification
    if extension in _EXTENSIONS:
        return _EXTENSIONS[extension]
    return Classification(Kind.GENERIC, declared_type or Media.OCTET_STREAM)
