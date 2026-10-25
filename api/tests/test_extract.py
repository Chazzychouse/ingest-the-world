"""Header-only extraction (ING-6): every format's metadata comes from a bounded number of bytes,
however large the file, and nothing a file contains can make a reader fetch more.

Files here are several times the byte budget, and the in-memory store counts what was really
read, independently of the reader's own tally.
"""

import io
import json
import struct
from collections.abc import Mapping
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rasterio
from hypothesis import given
from hypothesis import strategies as st
from rasterio.crs import CRS
from rasterio.transform import from_origin

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox
from ingest_api.ports import Container, StorageError
from ingest_api.worker.classify import Media
from ingest_api.worker.extract import MAX_EXTRACT_BYTES, BlobSource, Extraction, extract
from ingest_api.worker.formats import READERS, glb, las
from tests.fakes import Faults, MemoryBlobStore
from tests.invariants import check

RAW = Container.RAW
# Far bigger than the budget, so reading the whole file can't pass for reading a header.
BIG = 3 * MAX_EXTRACT_BYTES
UTM_13N = "EPSG:32613"
# A 4 km square near Denver, in UTM zone 13N.
ORIGIN_X, ORIGIN_Y, SIDE_METRES = 500_000.0, 4_500_000.0, 4096.0


def geotiff(tmp_path, *, crs: str | None = UTM_13N) -> bytes:
    """A tiled, uncompressed GeoTIFF bigger than the budget."""
    pixels = int((BIG**0.5) // 512 + 1) * 512
    path = tmp_path / "ortho.tif"
    profile: dict[str, Any] = {
        "driver": "GTiff",
        "width": pixels,
        "height": pixels,
        "count": 1,
        "dtype": "uint8",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "transform": from_origin(ORIGIN_X, ORIGIN_Y, SIDE_METRES / pixels, SIDE_METRES / pixels),
    }
    if crs:
        profile["crs"] = crs
    with rasterio.open(str(path), "w", **profile) as dataset:
        dataset.write(np.zeros((1, pixels, pixels), dtype="uint8"))
    return path.read_bytes()


def las_file(*, point_count: int = 50_000_000, wkt: str | None = None) -> bytes:
    """A LAS 1.4 header (and optionally a WKT CRS record) followed by BIG bytes of points."""
    vlr = b""
    if wkt is not None:
        body = wkt.encode() + b"\x00"
        vlr = las.VLR_HEADER.pack(0, b"LASF_Projection", las.OGC_WKT, len(body), b"") + body
    header = bytearray(las.LAS_14_HEADER)
    header[0:4] = las.SIGNATURE
    las.VERSION.pack_into(header, 24, 1, 4)
    las.LAYOUT.pack_into(
        header, 94, las.LAS_14_HEADER, las.LAS_14_HEADER + len(vlr), int(bool(vlr))
    )
    las.POINT_FORMAT.pack_into(header, 104, 6, 30, 0)
    max_x, max_y = ORIGIN_X + SIDE_METRES, ORIGIN_Y
    min_x, min_y = ORIGIN_X, ORIGIN_Y - SIDE_METRES
    las.BOUNDS.pack_into(header, 179, max_x, min_x, max_y, min_y, 1700.0, 1500.0)
    las.POINT_COUNT_14.pack_into(header, 247, point_count)
    return bytes(header) + vlr + bytes(BIG)


def glb_file(document: Mapping[str, object] | bytes, *, binary: int = BIG) -> bytes:
    data = document if isinstance(document, bytes) else json.dumps(document).encode()
    data += b" " * (-len(data) % 4)
    total = glb.HEADER.size + glb.CHUNK.size + len(data) + glb.CHUNK.size + binary
    return (
        glb.HEADER.pack(glb.MAGIC, 2, total)
        + glb.CHUNK.pack(len(data), glb.JSON_CHUNK)
        + data
        + struct.pack("<II", binary, 0x004E4942)  # "BIN\0"
        + bytes(binary)
    )


MODEL: dict[str, object] = {
    "asset": {"version": "2.0", "generator": "test-exporter"},
    "extensionsUsed": ["KHR_materials_unlit"],
    "accessors": [
        {"count": 8, "min": [-1, -2, 0], "max": [1, 2, 3]},
        {"count": 36},
        {"count": 4, "min": [5, 5, 5], "max": [6, 6, 6]},
    ],
    "meshes": [
        {"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]},
        {"primitives": [{"attributes": {"POSITION": 2}, "mode": 0}]},  # points: no triangles
    ],
    "nodes": [{"mesh": 0}, {"mesh": 1}],
    "materials": [{}],
}


def parquet_file(*, geo: Mapping[str, object] | None = None) -> bytes:
    rows = BIG // 8
    table = pa.table({"reading": pa.array(np.zeros(rows))})
    if geo is not None:
        table = table.append_column("geometry", pa.array([b"\x01"] * rows, pa.binary()))
        table = table.replace_schema_metadata({"geo": json.dumps(geo)})
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="none")
    return buffer.getvalue()


def _extract(store: MemoryBlobStore, media: Media, data: bytes) -> Extraction:
    props = store.put(RAW, "item", data)
    return extract(READERS.get(media), BlobSource(store, RAW, "item", props.size))


def _within_budget(store: MemoryBlobStore, extraction: Extraction) -> None:
    fetched = store.bytes_read.get((RAW, "item"), 0)
    check("ING-6", fetched <= MAX_EXTRACT_BYTES, f"extraction fetched {fetched} bytes")
    check("ING-6", fetched == extraction.metadata.bytes_read, "bytes_read misreports the fetch")


def _near(bbox: Bbox | None, expected: tuple[float, float, float, float]) -> bool:
    if bbox is None:
        return False
    actual = (bbox.west, bbox.south, bbox.east, bbox.north)
    return all(abs(a - e) < 1e-3 for a, e in zip(actual, expected, strict=True))


# Where the 4 km UTM square sits in WGS84: on the zone's central meridian, 4500 km north.
DENVER = (-105.0, 40.6139, -104.9516, 40.6509)


@pytest.mark.invariant("ING-6")
def test_a_geotiff_yields_crs_footprint_and_bands_from_its_header(tmp_path):
    store = MemoryBlobStore()
    result = _extract(store, Media.TIFF, geotiff(tmp_path))

    _within_budget(store, result)
    metadata = result.metadata
    assert metadata.status == "extracted" and metadata.crs == UTM_13N
    assert _near(metadata.bbox, DENVER)
    assert metadata.properties["raster:band_count"] == 1
    assert metadata.properties["raster:bands"] == [{"data_type": "uint8", "nodata": None}]


def test_a_raster_without_a_crs_has_no_footprint(tmp_path):
    result = _extract(MemoryBlobStore(), Media.TIFF, geotiff(tmp_path, crs=None))

    assert result.metadata.status == "extracted"
    assert (result.metadata.crs, result.metadata.bbox) == (None, None)


@pytest.mark.invariant("ING-6")
def test_a_las_header_yields_count_bounds_and_crs():
    store = MemoryBlobStore()
    result = _extract(store, Media.LAS, las_file(wkt=CRS.from_epsg(32613).to_wkt()))

    _within_budget(store, result)
    metadata = result.metadata
    assert metadata.status == "extracted" and metadata.crs == UTM_13N
    assert _near(metadata.bbox, DENVER)
    assert metadata.properties["pc:count"] == 50_000_000
    assert metadata.properties["pc:encoding"] == "las"
    assert metadata.properties["las:version"] == "1.4"


def test_a_las_file_without_a_crs_keeps_its_native_extent():
    result = _extract(MemoryBlobStore(), Media.LAS, las_file())

    assert (result.metadata.crs, result.metadata.bbox) == (None, None)
    assert result.metadata.properties["pc:extent"] == {
        "min": [ORIGIN_X, ORIGIN_Y - SIDE_METRES, 1500.0],
        "max": [ORIGIN_X + SIDE_METRES, ORIGIN_Y, 1700.0],
    }


@pytest.mark.invariant("ING-6")
def test_a_glb_is_described_from_its_json_chunk():
    store = MemoryBlobStore()
    result = _extract(store, Media.GLB, glb_file(MODEL))

    _within_budget(store, result)
    properties = result.metadata.properties
    assert result.metadata.status == "extracted" and result.metadata.bbox is None
    assert properties["mesh:vertices"] == 12
    assert properties["mesh:triangles"] == 12
    assert properties["mesh:extent"] == {"min": [-1, -2, 0], "max": [6, 6, 6]}
    assert properties["mesh:generator"] == "test-exporter"
    assert properties["mesh:extensions"] == ["KHR_materials_unlit"]


@pytest.mark.invariant("ING-6")
def test_geoparquet_is_vector_data_with_a_footprint():
    geo: dict[str, object] = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB", "bbox": [-105.1, 39.9, -104.8, 40.1]}},
    }
    store = MemoryBlobStore()
    result = _extract(store, Media.PARQUET, parquet_file(geo=geo))

    _within_budget(store, result)
    assert result.kind == Kind.VECTOR
    assert result.metadata.crs == "OGC:CRS84"
    assert _near(result.metadata.bbox, (-105.1, 39.9, -104.8, 40.1))
    assert result.metadata.properties["table:row_count"] == BIG // 8


def test_plain_parquet_stays_tabular():
    result = _extract(MemoryBlobStore(), Media.PARQUET, parquet_file())

    assert result.kind is None and result.metadata.bbox is None
    assert result.metadata.properties["table:columns"] == [{"name": "reading", "type": "double"}]


@pytest.mark.invariant("ING-6")
def test_a_header_too_large_to_read_is_over_budget_not_read():
    store = MemoryBlobStore()
    huge_json = b"{}" + b" " * (MAX_EXTRACT_BYTES + 1)
    result = _extract(store, Media.GLB, glb_file(huge_json, binary=0))

    _within_budget(store, result)
    assert result.metadata.status == "over-budget"
    assert result.metadata.properties == {}


def test_a_format_without_a_reader_is_unsupported_and_not_read():
    store = MemoryBlobStore()
    result = _extract(store, Media.PDF, b"%PDF-1.7" + bytes(BIG))

    assert result.metadata.status == "unsupported"
    assert store.bytes_read.get((RAW, "item"), 0) == 0


@pytest.mark.parametrize("media", [Media.TIFF, Media.GLB, Media.PARQUET, Media.LAS])
def test_a_storage_failure_while_reading_is_retried_not_recorded(tmp_path, media):
    """Even when GDAL or Arrow swallow the error, the job must be retried rather than cataloged
    as unreadable."""
    data = {
        Media.TIFF: lambda: geotiff(tmp_path),
        Media.GLB: lambda: glb_file(MODEL),
        Media.PARQUET: parquet_file,
        Media.LAS: las_file,
    }[media]()
    store = MemoryBlobStore()
    props = store.put(RAW, "item", data)
    store.faults = Faults(transient=1.0)

    with pytest.raises(StorageError):
        extract(READERS[media], BlobSource(store, RAW, "item", props.size))


SIGNATURES = {
    Media.TIFF: b"II*\x00",
    Media.GLB: b"glTF\x02\x00\x00\x00",
    Media.LAS: b"LASF",
    Media.PARQUET: b"PAR1",
}


@pytest.mark.invariant("ING-6")
@given(
    media=st.sampled_from(list(SIGNATURES)),
    body=st.binary(max_size=2048),
    padding=st.sampled_from([0, MAX_EXTRACT_BYTES * 2]),
)
def test_no_file_can_make_a_reader_crash_or_overspend(media, body, padding):
    """Whatever follows a signature, extraction ends in a status, within the budget."""
    store = MemoryBlobStore()
    data = SIGNATURES[media] + body + bytes(padding)
    if media == Media.PARQUET:
        data += b"PAR1"  # the trailer, so Arrow reads the footer length from the fuzz
    result = _extract(store, media, data)

    _within_budget(store, result)
    assert result.metadata.status in ("extracted", "unreadable", "over-budget")
