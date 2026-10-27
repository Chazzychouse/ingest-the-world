"""The mesh recipe, run inline on models written here: a textured model becomes a tileset Cesium's
validator passes, every tile with its own texture; it applies to the GLBs cataloging read; and the
same model always makes the same archive."""

import zipfile
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest

from ingest_api.items.kinds import Kind
from ingest_api.worker.classify import Media
from ingest_api.worker.extract import Budget, extract
from ingest_api.worker.formats import READERS
from ingest_api.worker.recipe import Source, Unrenderable
from ingest_api.worker.recipes import RECIPES
from ingest_api.worker.recipes.mesh import Tiles3D
from tests.fakes import InlineSandbox
from tests.invariants import check
from tests.meshes import textured_model, tiles, validated

RECIPE = Tiles3D()


def source(path: Path, kind: Kind = Kind.MESH, media: Media = Media.GLB) -> Source:
    data = path.read_bytes()
    fetch = partial(_slice, data)
    found = extract(
        READERS[Media.GLB], Budget(fetch, path.name, len(data)), InlineSandbox()
    ).metadata
    return Source(
        uuid4(),
        "c",
        kind,
        media,
        len(data),
        "0" * 64,
        fetch,
        found.status,
        found.crs,
        found.properties,
    )


def _slice(data: bytes, offset: int, length: int) -> bytes:
    return data[offset : offset + length]


def render(original: Path, tmp_path: Path) -> Path:
    scratch = tmp_path / f"render-{uuid4().hex}"
    scratch.mkdir()
    return RECIPE.render(original, scratch)


@pytest.mark.invariant("ING-14")
def test_a_textured_model_becomes_a_tileset_the_validators_pass(tmp_path):
    original = textured_model(tmp_path / "terrain.glb", side=330, pixels=1024)

    archive = render(original, tmp_path)

    report = validated(archive, tmp_path)
    problems = report["numErrors"] + report["numWarnings"]
    issues = report.get("issues")
    check("ING-14", problems == 0, f"the validator found {problems} problems: {issues}")


def test_every_tile_has_its_own_crop_of_the_texture(tmp_path):
    original = textured_model(tmp_path / "terrain.glb", side=330, pixels=1024)
    archive = render(original, tmp_path)

    with zipfile.ZipFile(archive) as z:
        sizes = [len(z.read(n)) for n in z.namelist() if n.startswith("tiles/")]
    found = tiles(archive, tmp_path)

    assert len(found) > 1
    assert max(sizes) < original.stat().st_size  # no tile carries the whole model's texture


def test_the_recipe_applies_to_a_glb_with_triangles_whose_header_read(tmp_path):
    original = textured_model(tmp_path / "terrain.glb")

    assert RECIPE.applies(source(original))
    assert [r.role for r in RECIPES if r.applies(source(original))] == ["tileset"]
    assert not RECIPE.applies(source(original, kind=Kind.RASTER))
    assert not RECIPE.applies(source(original, media=Media.OBJ))


def test_the_recipe_skips_a_glb_whose_header_did_not_read(tmp_path):
    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"glTF\x02\x00\x00\x00" + bytes(40))

    assert not RECIPE.applies(source(broken))


@pytest.mark.invariant("ING-7")
def test_the_same_model_makes_the_same_archive(tmp_path):
    original = textured_model(tmp_path / "terrain.glb", side=80)

    first = render(original, tmp_path).read_bytes()
    second = render(original, tmp_path).read_bytes()

    check("ING-7", RECIPE.deterministic and first == second, "the tileset archive differs")


def test_a_model_that_is_not_a_glb_is_unrenderable(tmp_path):
    original = tmp_path / "model.glb"
    original.write_bytes(b"PK\x03\x04" + bytes(100))

    with pytest.raises(Unrenderable):
        render(original, tmp_path)


def test_renders_leave_nothing_beside_the_original(tmp_path):
    original = textured_model(tmp_path / "terrain.glb")
    render(original, tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["terrain.glb"]
