"""Header-only extraction (ING-6): every format's metadata comes from a bounded number of bytes,
however large the file, and nothing a file contains can make a reader fetch more.

Files here are several times the byte budget, and the in-memory store counts what was really
read, independently of the reader's own tally.
"""

import io
import json
import sqlite3
import struct
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rasterio
from hypothesis import given
from hypothesis import strategies as st
from rasterio.crs import CRS
from rasterio.errors import NotGeoreferencedWarning
from rasterio.transform import from_origin

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox, Captured
from ingest_api.ports import Container, StorageError
from ingest_api.worker.classify import Media
from ingest_api.worker.extract import MAX_EXTRACT_BYTES, Budget, Extraction, extract
from ingest_api.worker.formats import READERS, glb, las
from tests.fakes import Faults, InlineSandbox, MemoryBlobStore
from tests.invariants import check

RAW = Container.RAW
# Far bigger than the budget, so reading the whole file can't pass for reading a header.
BIG = 3 * MAX_EXTRACT_BYTES
UTM_13N = "EPSG:32613"
# A 4 km square near Denver, in UTM zone 13N.
ORIGIN_X, ORIGIN_Y, SIDE_METRES = 500_000.0, 4_500_000.0, 4096.0


def geotiff(
    tmp_path, *, crs: str | None = UTM_13N, tags: Mapping[str, Mapping[str, str]] | None = None
) -> bytes:
    """A tiled, uncompressed GeoTIFF bigger than the budget, with metadata `tags` by domain
    ("" for the default one)."""
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
        for domain, values in (tags or {}).items():
            dataset.update_tags(ns=domain or None, **values)
    return path.read_bytes()


def las_file(
    *, point_count: int = 50_000_000, wkt: str | None = None, created: tuple[int, int] = (0, 0)
) -> bytes:
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
    las.CREATED.pack_into(header, 90, *created)
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


def geopackage(
    *,
    layers: int = 1,
    page_size: int = 1024,
    extent: tuple[float | None, ...] | None = None,
) -> bytes:
    """A GeoPackage written by SQLite itself, not by our reader's idea of the format: small
    pages, so many layers need interior B-tree pages and a CRS definition overflows its page,
    and a padding table that puts the file far over the budget."""
    wkt = CRS.from_epsg(32613).to_wkt(version="WKT2_2019")
    bounds = extent or (ORIGIN_X, ORIGIN_Y - SIDE_METRES, ORIGIN_X + SIDE_METRES, ORIGIN_Y)
    db = sqlite3.connect(":memory:")
    with db:
        db.execute(f"PRAGMA page_size = {page_size}")
        db.execute(
            """CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL PRIMARY KEY, organization TEXT NOT NULL,
            organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL,
            description TEXT)"""
        )
        db.execute(
            """CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY,
            data_type TEXT NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '',
            last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
            CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id)
                REFERENCES gpkg_spatial_ref_sys(srs_id))"""
        )
        db.execute("CREATE TABLE padding (data BLOB)")
        # Named by a definition only (organization "NONE"), so the WKT has to be read in full.
        db.execute(
            "INSERT INTO gpkg_spatial_ref_sys VALUES ('utm', 32613, 'NONE', 32613, ?, NULL)", (wkt,)
        )
        db.executemany(
            "INSERT INTO gpkg_contents (table_name, data_type, min_x, min_y, max_x, max_y, srs_id)"
            " VALUES (?, 'features', ?, ?, ?, ?, 32613)",
            [(f"layer_{i:03}", *bounds) for i in range(layers)],
        )
        db.execute("INSERT INTO padding VALUES (?)", (bytes(BIG),))
    data = db.serialize()
    db.close()
    return data


def shapefile(shape_type: int = 5, bounds=(1.0, 2.0, 3.0, 4.0), *, size: int = BIG) -> bytes:
    header = bytearray(100)
    struct.pack_into(">ii", header, 0, 9994, 0)
    struct.pack_into(">i", header, 24, size // 2)
    struct.pack_into("<ii4d", header, 28, 1000, shape_type, *bounds)
    return bytes(header) + bytes(size - 100)


def _budget(store: MemoryBlobStore, size: int) -> Budget:
    return Budget(partial(store.read_range, RAW, "item"), "item", size)


def _extract(store: MemoryBlobStore, media: Media, data: bytes) -> Extraction:
    props = store.put(RAW, "item", data)
    return extract(READERS.get(media), _budget(store, props.size), InlineSandbox())


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


UTC_TIME = "2023-05-31T09:00:00Z"


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (
            {
                "IMAGERY": {"ACQUISITIONDATETIME": UTC_TIME},
                "": {"TIFFTAG_DATETIME": "2024:01:01 00:00:00"},
            },
            UTC_TIME,
        ),
        ({"": {"TIFFTAG_DATETIME": "2023:06:01 10:20:30"}}, "2023-06-01T10:20:30Z"),
        ({"IMAGERY": {"ACQUISITIONDATETIME": "2023-05-31T11:00:00+02:00"}}, UTC_TIME),
        ({"": {"TIFFTAG_DATETIME": "0000:00:00 00:00:00"}}, None),
        ({"": {"TIFFTAG_DATETIME": "1066:10:14 09:00:00"}}, None),
        ({}, None),
    ],
    ids=["acquisition-first", "tiff-datetime", "offset", "zeroed", "implausible", "none"],
)
def test_a_raster_says_when_it_was_captured_if_its_header_does(tmp_path, tags, expected):
    data = geotiff(tmp_path, tags=tags)
    captured = _extract(MemoryBlobStore(), Media.TIFF, data).metadata.captured

    moment = datetime.fromisoformat(expected) if expected else None
    assert captured == (Captured(moment, moment) if moment else None)


@pytest.mark.parametrize(
    ("created", "day"),
    [((152, 2023), datetime(2023, 6, 1, tzinfo=UTC)), ((0, 0), None), ((366, 2023), None)],
    ids=["june-1", "unset", "no-day-366"],
)
def test_a_las_file_was_captured_on_its_creation_day(created, day):
    captured = _extract(MemoryBlobStore(), Media.LAS, las_file(created=created)).metadata.captured

    expected = Captured(day, day + timedelta(days=1, microseconds=-1)) if day else None
    assert captured == expected


@pytest.mark.invariant("ING-6")
def test_a_geopackage_yields_its_layers_crs_and_footprint_from_two_tables():
    store = MemoryBlobStore()
    result = _extract(store, Media.GEOPACKAGE, geopackage())

    _within_budget(store, result)
    metadata = result.metadata
    assert (metadata.status, result.kind, metadata.crs) == ("extracted", Kind.VECTOR, UTM_13N)
    assert _near(metadata.bbox, DENVER)
    assert metadata.properties["gpkg:layers"] == [
        {"name": "layer_000", "type": "features", "crs": UTM_13N}
    ]


def test_a_geopackage_with_many_layers_is_read_across_interior_pages():
    result = _extract(MemoryBlobStore(), Media.GEOPACKAGE, geopackage(layers=300))

    layers = result.metadata.properties["gpkg:layers"]
    assert isinstance(layers, list) and len(layers) == 64  # MAX_LAYERS, in table order
    assert _near(result.metadata.bbox, DENVER)


def test_a_geopackage_without_extents_has_no_footprint():
    result = _extract(MemoryBlobStore(), Media.GEOPACKAGE, geopackage(extent=(None,) * 4))

    assert (result.metadata.status, result.metadata.bbox) == ("extracted", None)


@pytest.mark.invariant("ING-6")
def test_a_shapefile_has_a_native_extent_but_no_footprint_without_its_prj():
    store = MemoryBlobStore()
    result = _extract(store, Media.SHAPEFILE, shapefile())

    _within_budget(store, result)
    metadata = result.metadata
    assert (metadata.status, result.kind, metadata.bbox, metadata.crs) == (
        "extracted",
        Kind.VECTOR,
        None,
        None,
    )
    assert metadata.properties == {"shp:shape_type": "polygon", "proj:bbox": [1.0, 2.0, 3.0, 4.0]}


def test_a_shapefile_whose_header_misstates_its_length_is_unreadable():
    data = shapefile()[:-2]

    assert _extract(MemoryBlobStore(), Media.SHAPEFILE, data).metadata.status == "unreadable"


def _geojson(document: object) -> bytes:
    return json.dumps(document).encode()


def _feature(geometry: object) -> dict[str, object]:
    return {"type": "Feature", "properties": {}, "geometry": geometry}


LINE = {"type": "LineString", "coordinates": [[-104.9, 40.65], [-104.95, 40.62]]}
FEATURES = {
    "type": "FeatureCollection",
    "features": [
        _feature({"type": "Point", "coordinates": [-105, 40.6]}),
        _feature({"type": "GeometryCollection", "geometries": [LINE]}),
        _feature(None),
    ],
}


@pytest.mark.invariant("ING-6")
def test_a_geojson_file_within_the_budget_yields_its_footprint():
    store = MemoryBlobStore()
    result = _extract(store, Media.GEOJSON, _geojson(FEATURES))

    _within_budget(store, result)
    metadata = result.metadata
    assert (metadata.status, metadata.crs) == ("extracted", "OGC:CRS84")
    assert metadata.bbox == Bbox(-105, 40.6, -104.9, 40.65)
    assert metadata.properties["geojson:features"] == 3


def test_a_legacy_geojson_crs_is_honoured():
    utm: dict[str, object] = {
        "type": "Point",
        "coordinates": [ORIGIN_X + SIDE_METRES / 2, ORIGIN_Y - SIDE_METRES / 2],
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32613"}},
    }

    metadata = _extract(MemoryBlobStore(), Media.GEOJSON, _geojson(utm)).metadata

    assert metadata.crs == UTM_13N and metadata.bbox is not None
    assert (
        DENVER[0] < metadata.bbox.west < DENVER[2] and DENVER[1] < metadata.bbox.south < DENVER[3]
    )


@pytest.mark.invariant("ING-6")
def test_a_geojson_file_over_the_budget_is_not_read():
    store = MemoryBlobStore()
    result = _extract(store, Media.GEOJSON, _geojson(FEATURES) + b" " * BIG)

    _within_budget(store, result)
    assert result.metadata.status == "over-budget"


def test_deeply_nested_geojson_is_unreadable_not_a_crash():
    data = b'{"type": "Point", "coordinates": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"

    assert _extract(MemoryBlobStore(), Media.GEOJSON, data).metadata.status == "unreadable"


def test_a_plain_png_is_a_raster_without_a_footprint(tmp_path):
    path = str(tmp_path / "plain.png")
    with (
        pytest.warns(NotGeoreferencedWarning),  # writing it warns; reading it must not
        rasterio.open(path, "w", driver="PNG", width=4, height=4, count=3, dtype="uint8") as png,
    ):
        png.write(np.zeros((3, 4, 4), dtype="uint8"))
    result = _extract(MemoryBlobStore(), Media.PNG, (tmp_path / "plain.png").read_bytes())

    assert (result.metadata.status, result.metadata.bbox) == ("extracted", None)
    assert result.metadata.properties["raster:band_count"] == 3


@pytest.mark.invariant("ING-6")
def test_a_compound_crs_is_named_by_its_parts_codes():
    """3DEP tiles carry a horizontal and a vertical CRS; their WKT has no code of its own."""
    compound = CRS.from_user_input("EPSG:26913+5703").to_wkt()
    result = _extract(MemoryBlobStore(), Media.LAS, las_file(wkt=compound))

    assert result.metadata.crs == "EPSG:26913+5703"
    assert _near(result.metadata.bbox, DENVER)


@pytest.mark.invariant("ING-6")
def test_a_tiff_whose_tiles_lie_past_its_end_is_unreadable(tmp_path):
    """A transfer cut short and committed anyway: the header is intact, the pixels aren't."""
    data = geotiff(tmp_path)
    result = _extract(MemoryBlobStore(), Media.TIFF, data[: len(data) // 2])

    assert result.metadata.status == "unreadable"


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
        extract(READERS[media], _budget(store, props.size), InlineSandbox())


SIGNATURES = {
    Media.TIFF: b"II*\x00",
    Media.GLB: b"glTF\x02\x00\x00\x00",
    Media.LAS: b"LASF",
    Media.PARQUET: b"PAR1",
    Media.GEOPACKAGE: b"SQLite format 3\x00",
    Media.SHAPEFILE: b"\x00\x00\x27\x0a",
    Media.GEOJSON: b'{"type": "FeatureCollection", "features": [',
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


GEOPACKAGE = geopackage(layers=40, page_size=512)


@pytest.mark.invariant("ING-6")
@given(st.lists(st.tuples(st.integers(0, 16 * 512 - 1), st.integers(0, 255)), max_size=8))
def test_no_corruption_of_a_geopackage_can_crash_its_reader_or_overspend(changes):
    """Bytes changed in the pages the reader walks (header, schema, both tables): the walk
    still ends in a status, within the budget."""
    data = bytearray(GEOPACKAGE)
    for offset, value in changes:
        data[offset] = value
    store = MemoryBlobStore()
    result = _extract(store, Media.GEOPACKAGE, bytes(data))

    _within_budget(store, result)
    assert result.metadata.status in ("extracted", "unreadable", "over-budget")
