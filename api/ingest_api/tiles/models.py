"""Response shapes for map tiles: TileJSON for a footprint view."""

import datetime as dt
from typing import Literal

from ingest_api.schema import ApiModel

TILEJSON_VERSION = "3.0.0"


class VectorLayer(ApiModel):
    id: str
    fields: dict[str, str]  # attribute name -> what it holds


class FootprintView(ApiModel):
    """TileJSON for a footprint view: its tile URL (relative to the API), and when it stops
    serving. A map past that opens a new view."""

    tilejson: Literal["3.0.0"] = TILEJSON_VERSION
    tiles: list[str]
    minzoom: int
    maxzoom: int
    vector_layers: list[VectorLayer]
    expires_at: dt.datetime
