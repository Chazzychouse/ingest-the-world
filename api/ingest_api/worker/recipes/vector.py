"""Vector derivatives, read by GDAL's OGR (through pyogrio) from the original's local copy.

- `geoparquet`: the original's features as GeoParquet 1.1 (WKB geometry, `geo` metadata), for
  GeoPackage, Shapefile and GeoJSON originals; a GeoParquet original serves as it is. Read and
  written a batch at a time, so memory stays at one batch however many features there are.

GDAL picks a Shapefile's driver by its extension, so the copy is linked under one in scratch; a
missing `.shx` index is rebuilt there, and a missing `.prj` leaves the CRS unknown, written as
`null` (GeoParquet reads an absent CRS as WGS84, which would mislabel projected coordinates).
Deterministic for a given GDAL and pyarrow (ING-7); a change that would change its output raises
its version.
"""

import json
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
import pyogrio.raw
from pyogrio.errors import DataLayerError, DataSourceError
from rasterio.crs import CRS
from rasterio.errors import CRSError

from ingest_api.items.kinds import Kind
from ingest_api.worker.classify import HEAD_BYTES, Media, classify
from ingest_api.worker.recipe import Source, Unrenderable

SOFTWARE: Mapping[str, str] = {
    "GDAL": pyogrio.__gdal_version_string__,
    "pyogrio": pyogrio.__version__,
    "pyarrow": pa.__version__,
}
GEOPARQUET_VERSION = "1.1.0"
GEOMETRY = "geometry"
# Features per batch read and per row group written.
BATCH_ROWS = 10_000
# Whose header reading leaves the file whole: read, or too big to read within the budget.
_READABLE = ("extracted", "over-budget")
_CONVERTED = (Media.GEOPACKAGE, Media.SHAPEFILE, Media.GEOJSON)
# OGR's geometry type names that GeoParquet's `geometry_types` admits, with or without " Z".
_GEOMETRY_TYPES = frozenset(
    {"Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"}
    | {"GeometryCollection"}
)
# GDAL options for the read: rebuild a Shapefile's missing index, and nothing else implicit.
_GDAL_OPTIONS = {"SHAPE_RESTORE_SHX": "YES"}


class GeoParquet:
    role, version, media_type, suffix = "geoparquet", 1, Media.PARQUET.value, ".parquet"
    title, stac_roles = "GeoParquet", ("data",)
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        return (
            source.kind == Kind.VECTOR
            and source.media_type in _CONVERTED
            and source.extraction in _READABLE
        )

    def render(self, original: Path, scratch: Path) -> Path:
        out = scratch / "geoparquet.parquet"
        with _features(_openable(original, scratch)) as (meta, reader):
            schema = _renamed(reader.schema, meta)
            geo = json.dumps(_geo_metadata(meta)).encode()
            with pq.ParquetWriter(
                out, schema.with_metadata({b"geo": geo}), compression="zstd"
            ) as w:
                for batch in reader:  # bounded: the layer's features, BATCH_ROWS at a time
                    w.write_batch(pa.RecordBatch.from_arrays(batch.columns, schema=w.schema))
        return out


def _openable(original: Path, scratch: Path) -> Path:
    """The path GDAL should open: the original, or for a Shapefile a link to it named `.shp`."""
    with original.open("rb") as file:
        head = file.read(HEAD_BYTES)
    if classify(head, original.name).media_type != Media.SHAPEFILE:
        return original
    linked = scratch / "original.shp"
    linked.symlink_to(original)
    return linked


@contextmanager
def _features(path: Path) -> Generator[tuple[dict[str, Any], pa.RecordBatchReader]]:
    """The first layer's metadata and its features, in batches, as Arrow with WKB geometry."""
    pyogrio.set_gdal_config_options(_GDAL_OPTIONS)
    try:
        with pyogrio.raw.open_arrow(path, batch_size=BATCH_ROWS, use_pyarrow=True) as opened:
            yield opened
    except (DataSourceError, DataLayerError, pa.ArrowException) as exc:
        raise Unrenderable(str(exc)) from exc


def _renamed(schema: pa.Schema, meta: Mapping[str, Any]) -> pa.Schema:
    """The layer's schema with its geometry column named `geometry`, as GeoParquet expects."""
    name = meta["geometry_name"] or "wkb_geometry"
    fields = [f.with_name(GEOMETRY) if f.name == name else f for f in schema]
    return pa.schema(fields, metadata=schema.metadata)


def _geo_metadata(meta: Mapping[str, Any]) -> dict[str, Any]:
    """GeoParquet's `geo` metadata for one WKB geometry column in the layer's CRS."""
    kind = str(meta["geometry_type"])
    types = [kind] if kind.removesuffix(" Z") in _GEOMETRY_TYPES else []  # [] means unknown
    column = {"encoding": "WKB", "geometry_types": types, "crs": _projjson(meta["crs"])}
    return {
        "version": GEOPARQUET_VERSION,
        "primary_column": GEOMETRY,
        "columns": {GEOMETRY: column},
    }


def _projjson(crs: str | None) -> dict[str, Any] | None:
    """The CRS as PROJJSON, or None when the file doesn't say (written as `null`: unknown)."""
    if crs is None:
        return None
    try:
        return CRS.from_user_input(crs).to_dict(projjson=True)
    except CRSError as exc:
        raise Unrenderable(f"unreadable CRS: {exc}") from exc
