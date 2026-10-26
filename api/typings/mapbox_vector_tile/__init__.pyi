# The part the tests use: decoding a tile into its layers' GeoJSON-like features.
from typing import Any

def decode(tile: bytes, **kwargs: Any) -> dict[str, dict[str, Any]]: ...
