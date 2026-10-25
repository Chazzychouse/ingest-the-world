"""glTF 2.0 binary (GLB): the JSON chunk, which describes every mesh, without the binary chunk.

A GLB is a 12-byte header, then a JSON chunk, then usually one binary chunk holding the
geometry. Counts come from accessors and the extent from POSITION accessors' `min`/`max`, which
the spec requires, so nothing past the JSON is read however large the model is. The extent is
in the model's own coordinates, before node transforms; a GLB carries no CRS of its own.
https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#binary-gltf-layout
"""

import json
import struct

from ingest_api.items.metadata import Json
from ingest_api.worker.extract import BlobSource, Found, Unreadable
from ingest_api.worker.formats.json_values import as_object, integer, numbers, objects, strings

HEADER = struct.Struct("<4sII")  # magic, version, total length
CHUNK = struct.Struct("<II")  # chunk length, chunk type
MAGIC = b"glTF"
JSON_CHUNK = 0x4E4F534A  # "JSON", little-endian
TRIANGLES = 4  # the default primitive mode
MAX_EXTENSIONS = 64


def read(source: BlobSource) -> Found:
    magic, version, _ = HEADER.unpack(source.read_exact(0, HEADER.size))
    if magic != MAGIC or version != 2:
        raise Unreadable(f"not a glTF 2.0 binary (version {version})")
    length, chunk_type = CHUNK.unpack(source.read_exact(HEADER.size, CHUNK.size))
    if chunk_type != JSON_CHUNK:
        raise Unreadable("the first GLB chunk is not JSON")
    document = _parse(source.read_exact(HEADER.size + CHUNK.size, length))
    return Found(properties=_properties(document))


def _parse(data: bytes) -> dict[str, object]:
    try:
        document = as_object(json.loads(data))
    except (ValueError, RecursionError) as exc:  # bad JSON or UTF-8; absurd nesting
        raise Unreadable(f"GLB JSON chunk: {exc}") from exc
    if document is None:
        raise Unreadable("GLB JSON chunk is not an object")
    return document


def _properties(document: dict[str, object]) -> dict[str, Json]:
    accessors = objects(document, "accessors")
    primitives = [p for mesh in objects(document, "meshes") for p in objects(mesh, "primitives")]
    vertices = triangles = 0
    lows: list[list[float]] = []
    highs: list[list[float]] = []
    for primitive in primitives:
        position = _accessor(accessors, as_object(primitive.get("attributes")), "POSITION")
        if position is None:
            continue
        count = integer(position, "count") or 0
        vertices += count
        triangles += _triangles(primitive, accessors, count)
        low, high = numbers(position, "min", 3), numbers(position, "max", 3)
        if low and high:
            lows.append(low)
            highs.append(high)
    asset = as_object(document.get("asset")) or {}
    properties: dict[str, Json] = {
        "mesh:meshes": len(objects(document, "meshes")),
        "mesh:primitives": len(primitives),
        "mesh:nodes": len(objects(document, "nodes")),
        "mesh:materials": len(objects(document, "materials")),
        "mesh:vertices": vertices,
        "mesh:triangles": triangles,
        "mesh:extensions": list(strings(document, "extensionsUsed", MAX_EXTENSIONS)),
    }
    if lows:
        properties["mesh:extent"] = {
            "min": [min(axis) for axis in zip(*lows, strict=True)],
            "max": [max(axis) for axis in zip(*highs, strict=True)],
        }
    if isinstance(generator := asset.get("generator"), str):
        properties["mesh:generator"] = generator[:255]
    return properties


def _accessor(
    accessors: list[dict[str, object]], parent: dict[str, object] | None, key: str
) -> dict[str, object] | None:
    """The accessor `parent[key]` refers to, if the index is valid."""
    index = integer(parent, key) if parent is not None else None
    return accessors[index] if index is not None and 0 <= index < len(accessors) else None


def _triangles(primitive: dict[str, object], accessors: list[dict[str, object]], count: int) -> int:
    mode = integer(primitive, "mode")
    if mode is not None and mode != TRIANGLES:
        return 0
    indices = _accessor(accessors, primitive, "indices")
    return (integer(indices, "count") or 0) // 3 if indices is not None else count // 3
