"""GeoPackage: its layers, their CRSs and its footprint, from `gpkg_contents` and
`gpkg_spatial_ref_sys` alone; no feature or tile is read.

Each layer's row in `gpkg_contents` may give its extent in its own CRS; the footprint is the
WGS84 box around every layer that does. https://www.geopackage.org/spec/
"""

import struct

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox, Json
from ingest_api.worker.extract import BlobSource, Found, Unreadable
from ingest_api.worker.formats import crs
from ingest_api.worker.formats.sqlite import Database, Value

MAX_LAYERS = 64
# A layer with no CRS: `srs_id` -1 is "undefined Cartesian", 0 "undefined geographic".
UNDEFINED = (-1, 0)


def read(source: BlobSource) -> Found:
    try:
        return _read(Database.open(source))
    except (struct.error, IndexError) as exc:
        # Offsets from a corrupt file pointing past a page or a record.
        raise Unreadable(f"GeoPackage structure: {exc}") from exc


def _read(database: Database) -> Found:
    systems = {
        row["srs_id"]: _crs_name(row)
        for row in database.table("gpkg_spatial_ref_sys")
        if isinstance(row.get("srs_id"), int)
    }
    layers: list[Json] = []
    boxes: list[Bbox] = []
    names: set[str] = set()
    for row in database.table("gpkg_contents")[:MAX_LAYERS]:
        native = systems.get(row.get("srs_id"))
        layers.append(
            {
                "name": _text(row.get("table_name")),
                "type": _text(row.get("data_type")),
                "crs": native,
            }
        )
        if native is not None:
            names.add(native)
            box = _footprint(native, row)
            if box is not None:
                boxes.append(box)
    properties: dict[str, Json] = {"gpkg:layers": layers}
    kind = Kind.RASTER if layers and all(_is_tiles(layer) for layer in layers) else Kind.VECTOR
    # One CRS names the file's; several leave it to each layer.
    only = next(iter(names)) if len(names) == 1 else None
    return Found(properties=properties, bbox=_union(boxes), crs=only, kind=kind)


def _crs_name(row: dict[str, Value]) -> str | None:
    if row.get("srs_id") in UNDEFINED:
        return None
    organization, code = row.get("organization"), row.get("organization_coordsys_id")
    if isinstance(organization, str) and organization.upper() == "EPSG" and isinstance(code, int):
        return crs.from_epsg(code)
    definition = row.get("definition")
    return crs.from_wkt(definition) if isinstance(definition, str) else None


def _footprint(native: str, row: dict[str, Value]) -> Bbox | None:
    bounds = [row.get(k) for k in ("min_x", "min_y", "max_x", "max_y")]
    numbers = [float(v) for v in bounds if isinstance(v, int | float)]
    if len(numbers) != 4 or numbers[0] > numbers[2] or numbers[1] > numbers[3]:
        return None
    return crs.footprint(native, *numbers)


def _union(boxes: list[Bbox]) -> Bbox | None:
    if not boxes:
        return None
    return Bbox(
        min(b.west for b in boxes),
        min(b.south for b in boxes),
        max(b.east for b in boxes),
        max(b.north for b in boxes),
    )


def _text(value: Value) -> str | None:
    return value if isinstance(value, str) else None


def _is_tiles(layer: Json) -> bool:
    return isinstance(layer, dict) and layer.get("type") in ("tiles", "2d-gridded-coverage")
