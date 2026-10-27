"""Reading GLBs for the tiler: what it reads back from models written here, and the malformed or
unsupported models it refuses, as Unrenderable, before reading their geometry."""

import json
import struct
from pathlib import Path

import numpy as np
import pytest

from ingest_api.worker.meshes.gltf import Glb
from ingest_api.worker.meshes.model import Part
from ingest_api.worker.recipe import Unrenderable
from tests.meshes import write_model

FLOAT, UBYTE, USHORT, UINT = 5126, 5121, 5123, 5125


def handmade(path: Path, document: dict[str, object], binary: bytes) -> Path:
    """A GLB of `document` and `binary`, as given: no checks, so it can be wrong."""
    text = json.dumps(document).encode()
    text += b" " * (-len(text) % 4)
    binary += b"\0" * (-len(binary) % 4)
    total = 12 + 8 + len(text) + 8 + len(binary)
    chunks = (
        struct.pack("<II", len(text), 0x4E4F534A)
        + text
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, total) + chunks)
    return path


def triangle(**overrides: object) -> tuple[dict[str, object], bytes]:
    """One triangle: three float positions, then three uint16 indices."""
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32).tobytes()
    indices = np.array([0, 1, 2], np.uint16).tobytes()
    document: dict[str, object] = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "accessors": [
            {"bufferView": 0, "componentType": FLOAT, "count": 3, "type": "VEC3"},
            {"bufferView": 1, "componentType": USHORT, "count": 3, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 36},
            {"buffer": 0, "byteOffset": 36, "byteLength": 6},
        ],
        "buffers": [{"byteLength": 44}],
    }
    document.update(overrides)
    return document, positions + indices


def test_a_written_model_reads_back_with_its_node_transform(tmp_path):
    positions = np.array([[0, 0, 0], [2, 0, 0], [0, 2, 0], [2, 2, 1]], np.float32)
    mesh = {0: Part(positions, None, None, np.array([[0, 1, 2], [1, 3, 2]], np.uint32))}
    path = write_model(tmp_path / "m.glb", mesh, offset=(100.0, -5.0, 2.5))

    with Glb(path) as model:
        (p,) = model.primitives
        local = model.read(p.positions, 0, 10)
        world = local @ p.transform[:3, :3].T + p.transform[:3, 3]
        assert world.tolist() == (positions + np.array([100.0, -5.0, 2.5])).tolist()
        assert model.read_indices(p, 0, 10).tolist() == [[0, 1, 2], [1, 3, 2]]


def test_rows_are_read_through_a_stride_and_normalized_as_gltf_says(tmp_path):
    document, _ = triangle()
    # Interleaved: a float position (12 bytes) then a normalized ubyte UV pair, stride 16.
    rows = b"".join(
        np.array(p, np.float32).tobytes() + bytes([u, v, 0, 0])
        for p, (u, v) in zip(
            [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [(0, 0), (255, 0), (0, 51)], strict=True
        )
    )
    document["accessors"] = [
        {"bufferView": 0, "componentType": FLOAT, "count": 3, "type": "VEC3"},
        {
            "bufferView": 0,
            "byteOffset": 12,
            "componentType": UBYTE,
            "normalized": True,
            "count": 3,
            "type": "VEC2",
        },
    ]
    document["bufferViews"] = [{"buffer": 0, "byteLength": 48, "byteStride": 16}]
    document["meshes"] = [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}}]}]
    path = handmade(tmp_path / "m.glb", document, rows)

    with Glb(path) as model:
        (p,) = model.primitives
        assert p.uvs is not None
        assert model.read(p.uvs, 0, 3).tolist() == [[0, 0], [1, 0], [0, 0.2]]
        assert model.read(p.positions, 1, 1).tolist() == [[1, 0, 0]]
        assert model.read_indices(p, 0, 1).tolist() == [[0, 1, 2]]  # no indices: in order


def test_nodes_are_walked_once_even_in_a_cycle(tmp_path):
    document, binary = triangle(
        nodes=[{"mesh": 0, "children": [1]}, {"mesh": 0, "children": [0], "translation": [5, 0, 0]}]
    )
    path = handmade(tmp_path / "m.glb", document, binary)

    with Glb(path) as model:
        assert [p.transform[0, 3] for p in model.primitives] == [0, 5]


@pytest.mark.invariant("ING-8")
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            {
                "accessors": [
                    {"bufferView": 0, "componentType": FLOAT, "count": 4, "type": "VEC3"},
                    {"bufferView": 1, "componentType": USHORT, "count": 3, "type": "SCALAR"},
                ]
            },
            "past its buffer view",
        ),
        (
            {
                "bufferViews": [
                    {"buffer": 0, "byteOffset": 0, "byteLength": 36},
                    {"buffer": 0, "byteOffset": 40, "byteLength": 600},
                ]
            },
            "outside the binary chunk",
        ),
        ({"buffers": [{"byteLength": 44, "uri": "elsewhere.bin"}]}, "outside the GLB"),
        ({"extensionsRequired": ["KHR_draco_mesh_compression"]}, "requires KHR_draco"),
        (
            {
                "accessors": [
                    {
                        "bufferView": 0,
                        "componentType": FLOAT,
                        "count": 3,
                        "type": "VEC3",
                        "sparse": {},
                    },
                    {"bufferView": 1, "componentType": USHORT, "count": 3, "type": "SCALAR"},
                ]
            },
            "sparse",
        ),
        ({"meshes": [{"primitives": [{"attributes": {"POSITION": 7}}]}]}, "doesn't exist"),
        ({"nodes": [{"mesh": 3}]}, "doesn't exist"),
        ({"scene": 4}, "doesn't exist"),
    ],
)
def test_a_malformed_model_is_refused_before_its_geometry_is_read(tmp_path, change, reason):
    document, binary = triangle(**change)
    path = handmade(tmp_path / "m.glb", document, binary)

    with pytest.raises(Unrenderable, match=reason):
        Glb(path).__exit__(None, None, None)


@pytest.mark.invariant("ING-8")
def test_an_index_past_the_vertices_is_refused_when_read(tmp_path):
    document, binary = triangle()
    path = handmade(
        tmp_path / "m.glb", document, binary[:36] + np.array([0, 1, 9], np.uint16).tobytes()
    )

    with Glb(path) as model, pytest.raises(Unrenderable, match="index past"):
        model.read_indices(model.primitives[0], 0, 1)


@pytest.mark.parametrize(
    "data", [b"", b"glTF", b"glTF\x01\x00\x00\x00" + bytes(16), b"PK\x03\x04" + bytes(40)]
)
def test_what_is_not_a_glb_is_refused(tmp_path, data):
    path = tmp_path / "m.glb"
    path.write_bytes(data)

    with pytest.raises(Unrenderable):
        Glb(path)
