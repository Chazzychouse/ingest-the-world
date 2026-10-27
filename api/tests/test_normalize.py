"""Meshes in other formats, converted through Assimp: the same triangles come out as a GLB, every
mesh recipe renders from them, and what Assimp can't read is Unrenderable. FBX has no fixture
here: the sample corpus carries one (`make e2e`)."""

import struct
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from ingest_api.items.kinds import Kind
from ingest_api.worker.classify import Media, classify
from ingest_api.worker.meshes import normalize
from ingest_api.worker.meshes.gltf import Glb
from ingest_api.worker.recipe import Source, Unrenderable
from ingest_api.worker.recipes import MESH
from tests.invariants import check
from tests.meshes import validated

# A unit square in two triangles, as each format writes it.
SQUARE = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], np.float32)
OBJ = (
    b"o square\nv 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n"
    b"vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf 1/1 2/2 3/3 4/4\n"
)
PLY = (
    b"ply\nformat ascii 1.0\nelement vertex 4\nproperty float x\nproperty float y\n"
    b"property float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n"
    b"0 0 0\n1 0 0\n1 1 0\n0 1 0\n4 0 1 2 3\n"
)
STL_ASCII = (
    b"solid square\n"
    b"facet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 1 1 0\nendloop\nendfacet\n"
    b"facet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 1 0\nvertex 0 1 0\nendloop\nendfacet\n"
    b"endsolid square\n"
)


def _stl_binary() -> bytes:
    facets = [((0, 0, 0), (1, 0, 0), (1, 1, 0)), ((0, 0, 0), (1, 1, 0), (0, 1, 0))]
    rows = b"".join(struct.pack("<12fH", 0, 0, 1, *a, *b, *c, 0) for a, b, c in facets)
    return b"\0" * 80 + struct.pack("<I", len(facets)) + rows


MODELS = {"obj": OBJ, "ply": PLY, "stl-ascii": STL_ASCII, "stl-binary": _stl_binary()}


def _scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / f"render-{uuid4().hex}"
    scratch.mkdir()
    return scratch


def _original(tmp_path: Path, data: bytes) -> Path:
    """The original as the derive stage copies it: named `original`, with no extension."""
    path = tmp_path / "original"
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("name", MODELS)
def test_each_format_becomes_a_glb_of_the_same_triangles(tmp_path, name):
    glb = normalize.to_glb(_original(tmp_path, MODELS[name]), _scratch(tmp_path))

    with Glb(glb) as model:
        corners = []
        for p in model.primitives:
            world = (
                model.read(p.positions, 0, p.positions.count) @ p.transform[:3, :3].T
                + p.transform[:3, 3]
            )
            corners.append(world[model.read_indices(p, 0, p.triangles).astype(np.int64)])
    triangles = np.concatenate(corners)
    assert len(triangles) == 2
    assert sorted(map(tuple, np.unique(triangles.reshape(-1, 3), axis=0).tolist())) == sorted(
        map(tuple, SQUARE.tolist())
    )


@pytest.mark.invariant("ING-14")
@pytest.mark.parametrize("name", ["obj", "stl-binary"])
def test_every_mesh_recipe_renders_a_converted_model(tmp_path, name):
    original = _original(tmp_path, MODELS[name])

    for recipe in MESH:
        out = recipe.render(original, _scratch(tmp_path))
        assert out.stat().st_size > 0
    tileset = next(r for r in MESH if r.role == "tileset").render(original, _scratch(tmp_path))
    report = validated(tileset, tmp_path)
    problems = report["numErrors"] + report["numWarnings"]
    check("ING-14", problems == 0, f"the validator found {problems} problems")


@pytest.mark.parametrize("data", [b"not a model at all, though it has words", b"", b"\0" * 300])
def test_what_assimp_cannot_read_is_unrenderable(tmp_path, data):
    with pytest.raises(Unrenderable):
        normalize.to_glb(_original(tmp_path, data), _scratch(tmp_path))


def _source(media: Media, size: int) -> Source:
    return Source(
        uuid4(), "c", Kind.MESH, media, size, "0" * 64, lambda o, n: b"", "unsupported", None, {}
    )


@pytest.mark.parametrize("media", [Media.OBJ, Media.FBX, Media.PLY, Media.STL])
def test_the_mesh_recipes_apply_to_models_assimp_reads_whole(media):
    small, big = _source(media, 1024), _source(media, normalize.MAX_BYTES + 1)

    assert [r.role for r in MESH if r.applies(small)] == ["tileset", "proxy", "thumbnail"]
    assert [r.role for r in MESH if r.applies(big)] == []


@pytest.mark.parametrize(
    ("head", "name", "media"),
    [
        (b"Kaydara FBX Binary  \x00\x1a\x00", "model.fbx", Media.FBX),
        (b"Kaydara FBX Binary  \x00\x1a\x00", "model.bin", Media.FBX),
        (b"; FBX 7.4.0 project file", "model.fbx", Media.FBX),
        (b"\0" * 84, "part.stl", Media.STL),
        (b"solid part", "part.stl", Media.STL),
    ],
)
def test_fbx_and_stl_are_classified_as_meshes(head, name, media):
    found = classify(head, name)

    assert (found.kind, found.media_type) == (Kind.MESH, media)
