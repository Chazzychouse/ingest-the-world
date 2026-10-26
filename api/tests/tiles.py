"""Reading the vector tiles the API serves, as a map would."""

from typing import Any

import mapbox_vector_tile

from ingest_api.items.store import FOOTPRINT_LAYER


def footprints(content: bytes) -> list[dict[str, Any]]:
    """The properties of each footprint in a tile; none for an empty tile."""
    if not content:
        return []
    layers = mapbox_vector_tile.decode(content)
    return [
        feature["properties"]
        for feature in layers.get(FOOTPRINT_LAYER, {"features": []})["features"]
    ]
