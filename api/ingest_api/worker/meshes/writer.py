"""Writing a mesh as a GLB: one node, one mesh, a primitive per material.

Positions are written relative to `centre` and the node translates them back, so they keep
float32's precision far from the origin (a model in a projected CRS sits hundreds of kilometres
out). Each material keeps its base colour and sidedness, and its texture only as the tile's own
crop of it (`textures.Baked`); everything else about a material is the viewer's default.
"""

import json
from collections.abc import Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

from ingest_api.worker.formats.glb import CHUNK, HEADER, JSON_CHUNK, MAGIC
from ingest_api.worker.meshes.gltf import BIN_CHUNK, Material
from ingest_api.worker.meshes.model import NO_MATERIAL, Mesh, Part
from ingest_api.worker.meshes.textures import Baked

GENERATOR = "ingest-the-world"
FLOAT, UNSIGNED_INT = 5126, 5125
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
LINEAR, LINEAR_MIPMAP_LINEAR, CLAMP_TO_EDGE = 9729, 9987, 33071
DEFAULT = Material((1.0, 1.0, 1.0, 1.0), None, double_sided=False)


class _Document:
    """The JSON and binary chunk of a GLB being built."""

    def __init__(self) -> None:
        self.json: dict[str, list[dict[str, object]]] = {
            k: [] for k in ("accessors", "bufferViews", "materials", "images", "textures")
        }
        self.binary = bytearray()

    def view(self, data: bytes, target: int | None = None) -> int:
        view: dict[str, object] = {
            "buffer": 0,
            "byteOffset": len(self.binary),
            "byteLength": len(data),
        }
        if target is not None:
            view["target"] = target
        self.binary += data + b"\0" * (-len(data) % 4)
        return _append(self.json["bufferViews"], view)

    def accessor(self, values: NDArray[np.float32] | NDArray[np.uint32], *, target: int) -> int:
        kind = {1: "SCALAR", 2: "VEC2", 3: "VEC3"}[1 if values.ndim == 1 else values.shape[1]]
        accessor: dict[str, object] = {
            "bufferView": self.view(np.ascontiguousarray(values).tobytes(), target),
            "componentType": UNSIGNED_INT if values.dtype == np.uint32 else FLOAT,
            "count": len(values),
            "type": kind,
        }
        return _append(self.json["accessors"], accessor)


def _append(items: list[dict[str, object]], item: dict[str, object]) -> int:
    items.append(item)
    return len(items) - 1


def glb(
    mesh: Mesh,
    *,
    centre: NDArray[np.float64],
    materials: Sequence[Material],
    baked: Mapping[int, Baked],
) -> bytes:
    """The GLB of `mesh`; `baked` holds each textured material's crop, by material."""
    doc = _Document()
    primitives = [
        _primitive(doc, part, centre, _material(materials, index), baked.get(index))
        for index, part in mesh.items()
        if len(part.triangles)
    ]
    document: dict[str, object] = {
        "asset": {"version": "2.0", "generator": GENERATOR},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": [float(c) for c in centre]}],
        "meshes": [{"primitives": primitives}],
        **{key: items for key, items in doc.json.items() if items},
        "buffers": [{"byteLength": len(doc.binary)}],
    }
    if doc.json["textures"]:
        document["samplers"] = [
            {"magFilter": LINEAR, "minFilter": LINEAR_MIPMAP_LINEAR}
            | {"wrapS": CLAMP_TO_EDGE, "wrapT": CLAMP_TO_EDGE}
        ]
    return _pack(document, bytes(doc.binary))


def _material(materials: Sequence[Material], index: int) -> Material:
    return materials[index] if index != NO_MATERIAL and 0 <= index < len(materials) else DEFAULT


def _primitive(
    doc: _Document, part: Part, centre: NDArray[np.float64], material: Material, baked: Baked | None
) -> dict[str, object]:
    positions = (part.positions.astype(np.float64) - centre).astype(np.float32)
    position = doc.accessor(positions, target=ARRAY_BUFFER)
    accessors = doc.json["accessors"]
    accessors[position]["min"] = [float(v) for v in positions.min(axis=0)]
    accessors[position]["max"] = [float(v) for v in positions.max(axis=0)]
    attributes = {"POSITION": position}
    if part.normals is not None:
        attributes["NORMAL"] = doc.accessor(part.normals, target=ARRAY_BUFFER)
    pbr: dict[str, object] = {
        "baseColorFactor": list(material.base_colour),
        "metallicFactor": 0.0,
        "roughnessFactor": 1.0,
    }
    if baked is not None:
        attributes["TEXCOORD_0"] = doc.accessor(baked.uvs, target=ARRAY_BUFFER)
        image = _append(
            doc.json["images"], {"bufferView": doc.view(baked.image), "mimeType": baked.media_type}
        )
        texture = _append(doc.json["textures"], {"source": image, "sampler": 0})
        pbr["baseColorTexture"] = {"index": texture}
    entry: dict[str, object] = {"pbrMetallicRoughness": pbr, "doubleSided": material.double_sided}
    return {
        "attributes": attributes,
        "indices": doc.accessor(part.triangles.reshape(-1), target=ELEMENT_ARRAY_BUFFER),
        "material": _append(doc.json["materials"], entry),
    }


def _pack(document: dict[str, object], binary: bytes) -> bytes:
    text = json.dumps(document, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    total = HEADER.size + CHUNK.size + len(text) + CHUNK.size + len(binary)
    return b"".join(
        [
            HEADER.pack(MAGIC, 2, total),
            CHUNK.pack(len(text), JSON_CHUNK),
            text,
            CHUNK.pack(len(binary), BIN_CHUNK),
            binary,
        ]
    )
