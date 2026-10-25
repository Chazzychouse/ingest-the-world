"""First-pass classification from a file's first bytes and its name."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingest_api.items.kinds import Kind
from ingest_api.worker.classify import HEAD_BYTES, Classification, Media, classify


@pytest.mark.parametrize(
    ("head", "filename", "expected"),
    [
        (b"II*\x00rest", "ortho.tif", (Kind.RASTER, "image/tiff")),
        (b"MM\x00*rest", "ORTHO.TIFF", (Kind.RASTER, "image/tiff")),
        (b"II+\x00", "big.tif", (Kind.RASTER, "image/tiff")),
        (b"LASF\x00\x00", "scan.laz", (Kind.POINTCLOUD, "application/vnd.laszip")),
        (b"LASF\x00\x00", "scan.las", (Kind.POINTCLOUD, "application/vnd.las")),
        (b"glTF\x02\x00\x00\x00", "site.glb", (Kind.MESH, "model/gltf-binary")),
        (b"PAR1", "readings.parquet", (Kind.TABULAR, "application/vnd.apache.parquet")),
        (b"SQLite format 3\x00", "roads.gpkg", (Kind.VECTOR, "application/geopackage+sqlite3")),
        (b"%PDF-1.7", "report.pdf", (Kind.DOCUMENT, "application/pdf")),
        (b'{"type": "FeatureCollection"', "parcels.geojson", (Kind.VECTOR, "application/geo+json")),
        (b"v 0 0 0\n", "model.OBJ", (Kind.MESH, "model/obj")),
        (b"device,ts,value\n", "bio.csv", (Kind.TABULAR, "text/csv")),
        # Headers beat names that lie.
        (b"glTF\x02\x00\x00\x00", "renamed.tif", (Kind.MESH, "model/gltf-binary")),
    ],
)
def test_known_formats(head, filename, expected):
    assert classify(head, filename) == Classification(*expected)


def test_anything_else_is_generic_with_its_declared_type():
    assert classify(b"\x00\x01", "blob.bin") == Classification(Kind.GENERIC, Media.OCTET_STREAM)
    assert classify(b"\x00\x01", "x.dwg", "image/vnd.dwg") == Classification(
        Kind.GENERIC, "image/vnd.dwg"
    )


@given(head=st.binary(max_size=2 * HEAD_BYTES), filename=st.text(max_size=100))
def test_classifies_any_input_from_its_head_alone(head, filename):
    assert classify(head, filename) == classify(head[:HEAD_BYTES], filename)
