"""Response shapes for map tiles: TileJSON, for a footprint view and for a raster's preview."""

import datetime as dt
from typing import Literal

from ingest_api.schema import ApiModel

TILEJSON_VERSION = "3.0.0"


class TileJson(ApiModel):
    tilejson: Literal["3.0.0"] = TILEJSON_VERSION
    tiles: list[str]
    minzoom: int
    maxzoom: int


class VectorLayer(ApiModel):
    id: str
    fields: dict[str, str]  # attribute name -> what it holds


class FootprintView(TileJson):
    """TileJSON for a footprint view: its tile URL (relative to the API), and when it stops
    serving. A map past that opens a new view."""

    vector_layers: list[VectorLayer]
    expires_at: dt.datetime


class RasterTiles(TileJson):
    """TileJSON for a raster's preview, from the tiler: its tile URL, and where it lies."""

    bounds: tuple[float, float, float, float]  # west, south, east, north
