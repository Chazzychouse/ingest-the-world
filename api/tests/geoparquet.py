"""Checking a GeoParquet file as a reader independent of our recipe would: its `geo` metadata
against the GeoParquet project's own JSON Schema, and every geometry decoded by shapely.

Both schemas are vendored unchanged, so validating never reaches the network: GeoParquet 1.1.0's
(Apache-2.0) from
https://raw.githubusercontent.com/opengeospatial/geoparquet/v1.1.0/format-specs/schema.json and
the PROJJSON 0.7 schema it refers to for CRSs (MIT), from its own `$id`.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from referencing import Registry, Resource

DATA = Path(__file__).parent / "data"
SCHEMA = DATA / "geoparquet-1.1.0.schema.json"
PROJJSON = DATA / "projjson-0.7.schema.json"


def _validator() -> jsonschema.Draft7Validator:
    projjson = Resource.from_contents(json.loads(PROJJSON.read_text()))
    registry = Registry().with_resource(projjson.id() or "", projjson)
    return jsonschema.Draft7Validator(json.loads(SCHEMA.read_text()), registry=registry)


def geo_metadata(path: Path) -> dict[str, Any]:
    return json.loads(pq.read_schema(path).metadata[b"geo"])


def valid_geoparquet(path: Path) -> pa.Table:
    """The file's table, once its `geo` metadata validates and every geometry decodes; raises
    `jsonschema.ValidationError` or shapely's error otherwise."""
    geo = geo_metadata(path)
    _validator().validate(geo)
    table = pq.read_table(path)
    for name, column in geo["columns"].items():
        if column["encoding"] != "WKB":
            raise ValueError(f"{name}: unexpected encoding {column['encoding']}")
        shapely.from_wkb(table.column(name).to_pylist())
    return table
