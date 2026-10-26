"""Reading the vector tiles the API serves, as a map would."""

from typing import Any

import mapbox_vector_tile

from ingest_api.items.store import FOOTPRINT_LAYER


def features(content: bytes) -> list[dict[str, Any]]:
    """Each footprint in a tile, with its geometry and properties; none for an empty tile."""
    if not content:
        return []
    layers = mapbox_vector_tile.decode(content)
    return list(layers.get(FOOTPRINT_LAYER, {"features": []})["features"])


def footprints(content: bytes) -> list[dict[str, Any]]:
    """The properties of each footprint in a tile."""
    return [feature["properties"] for feature in features(content)]
