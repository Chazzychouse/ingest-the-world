"""The mesh recipe, run inline on models written here: a textured model becomes a tileset Cesium's
validator passes, every tile with its own texture; it applies to the GLBs cataloging read; and the
same model always makes the same archive."""

import zipfile
from functools import partial
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
import rasterio
from hypothesis import HealthCheck, given, settings

from ingest_api.items.kinds import Kind
from ingest_api.worker.classify import Media
from ingest_api.worker.extract import Budget, extract
from ingest_api.worker.formats import READERS
from ingest_api.worker.meshes.clustering import diagonal
from ingest_api.worker.meshes.coarse import coarse
from ingest_api.worker.meshes.gltf import Glb
from ingest_api.worker.recipe import Source, Unrenderable
from ingest_api.worker.recipes import MESH, RECIPES
from ingest_api.worker.recipes.mesh import PROXY_TRIANGLES, THUMBNAIL_PX, Proxy, Tiles3D
from ingest_api.worker.recipes.mesh import Thumbnail as MeshThumbnail
from tests.fakes import InlineSandbox
from tests.invariants import check
from tests.meshes import (
    meshes,
    original_triangles,
    textured_model,
    tiles,
    validated,
    write_model,
)

RECIPE = Tiles3D()
# Coarse models, like thumbnails, have no place on a map.
pytestmark = pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")


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
    assert [r.role for r in RECIPES if r.applies(source(original))] == [
        "tileset",
        "proxy",
        "thumbnail",
    ]
    assert not RECIPE.applies(source(original, kind=Kind.RASTER))
    assert not RECIPE.applies(source(original, media=Media.TIFF))


def test_the_recipe_skips_a_glb_whose_header_did_not_read(tmp_path):
    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"glTF\x02\x00\x00\x00" + bytes(40))

    assert not RECIPE.applies(source(broken))


def test_a_model_that_is_not_a_glb_is_unrenderable(tmp_path):
    original = tmp_path / "model.glb"
    original.write_bytes(b"PK\x03\x04" + bytes(100))

    with pytest.raises(Unrenderable):
        render(original, tmp_path)


def test_renders_leave_nothing_beside_the_original(tmp_path):
    original = textured_model(tmp_path / "terrain.glb")
    render(original, tmp_path)

    for recipe in MESH:
        recipe.render(original, _scratch(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["terrain.glb"]


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(mesh=meshes())
def test_a_coarse_model_stays_within_a_cell_of_the_model(tmp_path, mesh):
    """Every vertex of the coarse model is within its grid's cell diagonal of the model's."""
    path = write_model(tmp_path / "m.glb", mesh)
    original = np.unique(original_triangles(mesh).reshape(-1, 3), axis=0)

    with Glb(path) as model:
        found, (low, high) = coarse(model, cells=8, max_triangles=10**6)

    cell = float((high - low).max()) / 8
    for part in found.values():
        vertices = part.positions.astype(np.float64)
        nearest = np.min(np.linalg.norm(vertices[:, None] - original[None], axis=2), axis=1)
        assert nearest.max() <= diagonal(cell) + 1e-4
        assert part.triangles.max(initial=0) < len(vertices)


def test_the_proxy_is_one_small_glb_of_the_whole_model(tmp_path):
    original = textured_model(tmp_path / "terrain.glb", side=330, pixels=1024)

    proxy = Proxy().render(original, _scratch(tmp_path))

    with Glb(proxy) as model:
        triangles = sum(p.triangles for p in model.primitives)
        assert 0 < triangles <= PROXY_TRIANGLES
        assert model.image(0) is not None  # its texture, scaled
    assert proxy.stat().st_size < original.stat().st_size / 4


def test_a_thumbnail_shows_the_model_on_a_clear_ground(tmp_path):
    original = textured_model(tmp_path / "terrain.glb")

    path = MeshThumbnail().render(original, _scratch(tmp_path))

    with rasterio.open(str(path)) as png:
        assert (png.count, png.width, png.height) == (4, THUMBNAIL_PX, THUMBNAIL_PX)
        alpha = png.read([4])[0]
    assert alpha[0, 0] == 0 and alpha[THUMBNAIL_PX // 2, THUMBNAIL_PX // 2] == 255


@pytest.mark.invariant("ING-7")
@pytest.mark.parametrize("recipe", MESH, ids=lambda r: r.role)
def test_every_mesh_recipe_renders_the_same_bytes_twice(tmp_path, recipe):
    original = textured_model(tmp_path / "terrain.glb", side=60)

    first = recipe.render(original, _scratch(tmp_path)).read_bytes()
    second = recipe.render(original, _scratch(tmp_path)).read_bytes()

    check("ING-7", recipe.deterministic and first == second, f"{recipe.role} differs")


def _scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / f"render-{uuid4().hex}"
    scratch.mkdir()
    return scratch
