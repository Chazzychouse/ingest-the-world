"""First-pass classification: what kind of file this is, from its first bytes and its name.

Magic bytes win over the extension, since names lie more often than headers. Formats with no
signature (text formats, OBJ) fall back to the extension. Anything else is `generic`, which
the pipeline still catalogs. Refining ZIP bundles by their central directory comes with
header extraction (ING-6).
"""

from dataclasses import dataclass
from pathlib import PurePosixPath

from ingest_api.items.kinds import Kind

# How much of the file classification looks at; the pipeline reads no more for it.
HEAD_BYTES = 4096
OCTET_STREAM = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class Classification:
    kind: Kind
    media_type: str


_SIGNATURES: tuple[tuple[bytes, Classification], ...] = (
    (b"II*\x00", Classification(Kind.RASTER, "image/tiff")),
    (b"MM\x00*", Classification(Kind.RASTER, "image/tiff")),
    (b"II+\x00", Classification(Kind.RASTER, "image/tiff")),  # BigTIFF
    (b"\x89PNG\r\n\x1a\n", Classification(Kind.RASTER, "image/png")),
    (b"\xff\xd8\xff", Classification(Kind.RASTER, "image/jpeg")),
    (b"glTF", Classification(Kind.MESH, "model/gltf-binary")),
    (b"ply\n", Classification(Kind.MESH, "model/ply")),
    (b"ASTM-E57", Classification(Kind.POINTCLOUD, "model/e57")),
    (b"PAR1", Classification(Kind.TABULAR, "application/vnd.apache.parquet")),
    (b"SQLite format 3\x00", Classification(Kind.VECTOR, "application/geopackage+sqlite3")),
    (b"\x00\x00\x27\x0a", Classification(Kind.VECTOR, "application/vnd.shp")),
    (b"%PDF-", Classification(Kind.DOCUMENT, "application/pdf")),
)

# LAS and LAZ share the `LASF` signature; the extension says whether it's compressed.
_LAS = b"LASF"
_LAZ = Classification(Kind.POINTCLOUD, "application/vnd.laszip")
_LAS_PLAIN = Classification(Kind.POINTCLOUD, "application/vnd.las")

_EXTENSIONS: dict[str, Classification] = {
    ".geojson": Classification(Kind.VECTOR, "application/geo+json"),
    ".gltf": Classification(Kind.MESH, "model/gltf+json"),
    ".obj": Classification(Kind.MESH, "model/obj"),
    ".csv": Classification(Kind.TABULAR, "text/csv"),
    ".jsonl": Classification(Kind.TABULAR, "application/jsonl"),
    ".txt": Classification(Kind.DOCUMENT, "text/plain"),
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
    return Classification(Kind.GENERIC, declared_type or OCTET_STREAM)
