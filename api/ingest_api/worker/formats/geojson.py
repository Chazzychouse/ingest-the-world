"""GeoJSON: feature count and footprint. GeoJSON has no header, so the whole document is read,
which the budget allows only for files up to `MAX_EXTRACT_BYTES`; anything larger is
`over-budget` (ING-6) and gets its footprint, if ever, from a derive step.

Coordinates are WGS84 longitude and latitude (RFC 7946). A pre-2016 `crs` member naming another
CRS is honoured. https://www.rfc-editor.org/rfc/rfc7946
"""

import json
import math

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox, Json
from ingest_api.worker.extract import BlobSource, Found, Unreadable
from ingest_api.worker.formats import crs
from ingest_api.worker.formats.json_values import as_list, as_number, as_object, objects

CRS84 = "OGC:CRS84"
GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
}
# Nested lists walked to find positions: plenty for any geometry, and a bound for one that lies.
MAX_NODES = 2_000_000


def read(source: BlobSource) -> Found:
    try:
        document = as_object(json.loads(source.read_exact(0, source.size)))
    except (ValueError, RecursionError) as exc:
        raise Unreadable(f"not JSON: {exc}") from exc
    if document is None:
        raise Unreadable("not a JSON object")
    kind = document.get("type")
    if kind == "FeatureCollection":
        features = objects(document, "features")
        geometries: list[object] = [f.get("geometry") for f in features]
    elif kind == "Feature":
        features, geometries = [document], list[object]([document.get("geometry")])
    elif kind in GEOMETRY_TYPES:
        features, geometries = [], list[object]([document])
    else:
        raise Unreadable(f"GeoJSON type {kind!r}")
    properties: dict[str, Json] = {"geojson:features": len(features)}
    extent = _extent(geometries)
    if extent is None:
        return Found(properties=properties, kind=Kind.VECTOR)
    native = _crs(document)
    if native is None:
        return Found(properties=properties, kind=Kind.VECTOR)
    bbox = Bbox.checked(*extent) if native == CRS84 else crs.footprint(native, *extent)
    return Found(properties=properties, bbox=bbox, crs=native, kind=Kind.VECTOR)


def _crs(document: dict[str, object]) -> str | None:
    """CRS84 unless a legacy `crs` member names another CRS by name; None if it names one PROJ
    doesn't know, since the numbers then can't be placed."""
    legacy = as_object(document.get("crs"))
    if legacy is None:
        return CRS84
    named = as_object(legacy.get("properties"))
    value = named.get("name") if named else None
    return crs.from_user_input(value) if isinstance(value, str) else None


def _extent(geometries: list[object]) -> tuple[float, float, float, float] | None:
    """The bounds of every position in `geometries`, walked without recursion: collections
    and nested coordinate lists go on the stack, positions update the bounds."""
    west = south = math.inf
    east = north = -math.inf
    pending = list(geometries)
    for _ in range(MAX_NODES):
        if not pending:
            break
        node = pending.pop()
        if (shape := as_object(node)) is not None:
            if shape.get("type") == "GeometryCollection":
                pending.extend(objects(shape, "geometries"))
            else:
                pending.append(shape.get("coordinates"))
            continue
        values = as_list(node)
        if not values:
            continue
        if isinstance(values[0], list):
            pending.extend(values)
            continue
        x, y = as_number(values[0]), as_number(values[1]) if len(values) > 1 else None
        if x is not None and y is not None:
            west, south, east, north = min(west, x), min(south, y), max(east, x), max(north, y)
    else:
        raise Unreadable(f"more than {MAX_NODES} geometry parts")
    return None if west > east else (west, south, east, north)
