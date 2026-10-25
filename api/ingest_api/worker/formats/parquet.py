"""Parquet and GeoParquet: row count, columns and, for GeoParquet, the footprint, from the
footer alone.

A Parquet file ends with its metadata and an 8-byte trailer saying how long the metadata is, so
two range reads near the end describe a file of any size. GeoParquet adds a `geo` key whose
JSON names the geometry column, its CRS (PROJJSON; absent means OGC:CRS84) and, optionally,
its bbox. A file with one is vector data, not just a table.
https://github.com/opengeospatial/geoparquet/blob/main/format-specs/geoparquet.md
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Json
from ingest_api.worker.extract import BlobSource, Found, Unreadable
from ingest_api.worker.formats import crs
from ingest_api.worker.formats.json_values import as_object, numbers

MAX_COLUMNS = 256
GEO_KEY = b"geo"
CRS84 = "OGC:CRS84"


def read(source: BlobSource) -> Found:
    try:
        with source.open() as file:
            metadata = pq.ParquetFile(file).metadata
            schema = metadata.schema.to_arrow_schema()
    except (pa.ArrowException, OSError) as exc:
        raise Unreadable(f"Parquet footer: {exc}") from exc
    properties: dict[str, Json] = {
        "table:row_count": metadata.num_rows,
        "table:columns": [
            {"name": f.name, "type": str(f.type)} for f in list(schema)[:MAX_COLUMNS]
        ],
        "table:column_count": len(schema),
    }
    geo = _geo((schema.metadata or {}).get(GEO_KEY))
    if geo is None:
        return Found(properties=properties)
    return _geoparquet(properties, geo)


def _geo(raw: bytes | None) -> dict[str, object] | None:
    """The primary geometry column's GeoParquet metadata, if the file has any."""
    if raw is None:
        return None
    try:
        geo = as_object(json.loads(raw))
    except (ValueError, RecursionError):
        return None
    columns = as_object(geo.get("columns")) if geo else None
    primary = geo.get("primary_column") if geo else None
    if columns is None or not isinstance(primary, str):
        return None
    column = as_object(columns.get(primary))
    return None if column is None else {"name": primary, **column}


def _geoparquet(properties: dict[str, Json], column: dict[str, object]) -> Found:
    properties["geoparquet:geometry_column"] = str(column["name"])
    projjson = column.get("crs", CRS84)
    if projjson is None:  # explicitly undefined: coordinates in an unknown system
        return Found(properties=properties, kind=Kind.VECTOR)
    native = crs.from_user_input(projjson if isinstance(projjson, str) else json.dumps(projjson))
    box = numbers(column, "bbox", 4)
    if native is None or box is None:
        return Found(properties=properties, crs=native, kind=Kind.VECTOR)
    bbox = crs.footprint(native, *box)
    return Found(properties=properties, bbox=bbox, crs=native, kind=Kind.VECTOR)
