"""The raster recipes, run inline on rasters GDAL writes here: what each derivative holds, which
originals each applies to (judged from what the raster reader recorded), and that the same
original always renders the same bytes."""

from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Metadata
from ingest_api.worker.classify import Media
from ingest_api.worker.extract import Budget, extract
from ingest_api.worker.formats import READERS
from ingest_api.worker.recipe import Recipe, Source, Unrenderable
from ingest_api.worker.recipes import RASTER, RECIPES
from ingest_api.worker.recipes.raster import THUMBNAIL_PX, Cog, Thumbnail
from tests.fakes import InlineSandbox
from tests.invariants import check

UTM_13N = "EPSG:32613"
# Thumbnails, like photos, have no place on the map.
pytestmark = pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")


def write_raster(
    path: Path,
    pixels: np.ndarray,
    *,
    crs: str | None = UTM_13N,
    driver: str = "GTiff",
    **profile: Any,
) -> Path:
    """`pixels` (bands, rows, columns) as a raster at `path`, 10 m pixels if it has a CRS."""
    count, height, width = pixels.shape
    if crs:
        profile |= {"crs": crs, "transform": from_origin(500_000, 4_400_000, 10, 10)}
    with rasterio.open(
        str(path), "w", driver=driver, width=width, height=height, count=count,
        dtype=pixels.dtype.name, **profile,
    ) as dataset:  # fmt: skip
        dataset.write(pixels)
    return path


def rgb(height: int, width: int) -> np.ndarray:
    return np.random.default_rng(7).integers(0, 255, (3, height, width), dtype=np.uint8)


def _slice(data: bytes, offset: int, length: int) -> bytes:
    return data[offset : offset + length]


def metadata(path: Path, media: Media = Media.TIFF) -> Metadata:
    """What the raster reader records about the file, as cataloging would."""
    data = path.read_bytes()
    budget = Budget(partial(_slice, data), path.name, len(data))
    return extract(READERS[media], budget, InlineSandbox()).metadata


def source(path: Path, kind: Kind = Kind.RASTER, media: Media = Media.TIFF) -> Source:
    found, size = metadata(path, media), path.stat().st_size
    fetch = partial(_slice, path.read_bytes())
    return Source(
        uuid4(), "c", kind, media, size, "0" * 64, fetch, found.status, found.crs, found.properties
    )


def render(recipe: Recipe, original: Path, tmp_path: Path) -> Path:
    scratch = tmp_path / f"{recipe.role}-{uuid4().hex}"
    scratch.mkdir()
    return recipe.render(original, scratch)


def test_a_cog_holds_the_original_pixels_in_tiles_with_overviews(tmp_path):
    original = write_raster(tmp_path / "ortho.tif", rgb(1500, 2000))

    cog = render(Cog(), original, tmp_path)

    with rasterio.open(str(original)) as before, rasterio.open(str(cog)) as after:
        assert after.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        assert after.block_shapes[0] == (512, 512)
        assert after.overviews(1)
        assert (after.crs, after.transform) == (before.crs, before.transform)
        assert np.array_equal(after.read(), before.read())


def test_the_cog_recipe_skips_cloud_optimized_rasters_and_those_with_no_place(tmp_path):
    plain = write_raster(tmp_path / "plain.tif", rgb(1024, 1024))
    tiled = write_raster(
        tmp_path / "tiled.tif", rgb(1024, 1024), tiled=True, blockxsize=256, blockysize=256
    )
    with rasterio.open(str(tiled), "r+") as dataset:
        dataset.build_overviews([2, 4])
    made = render(Cog(), plain, tmp_path)
    unplaced = write_raster(tmp_path / "photo.png", rgb(64, 64), crs=None, driver="PNG")

    found = {
        p.name: metadata(p).properties.get("raster:cloud_optimized") for p in (plain, tiled, made)
    }
    assert found == {"plain.tif": False, "tiled.tif": True, "cog.tif": True}
    assert Cog().applies(source(plain))
    assert not Cog().applies(source(tiled)) and not Cog().applies(source(made))
    assert not Cog().applies(source(unplaced, media=Media.PNG))
    assert not Cog().applies(source(plain, Kind.TABULAR))
    assert Thumbnail().applies(source(unplaced, media=Media.PNG))
    assert not Thumbnail().applies(source(plain, Kind.VECTOR))


def test_no_recipe_applies_to_a_raster_whose_header_did_not_read(tmp_path):
    """A truncated COG's overviews may survive, and would thumbnail as if it were whole."""
    whole = render(Cog(), write_raster(tmp_path / "ortho.tif", rgb(2048, 2048)), tmp_path)
    truncated = tmp_path / "truncated.tif"
    truncated.write_bytes(whole.read_bytes()[: whole.stat().st_size // 2])

    found = source(truncated)
    assert found.extraction == "unreadable"
    assert [r.role for r in RECIPES if r.applies(found)] == []


def test_a_thumbnail_fits_the_longer_side_and_keeps_the_colours(tmp_path):
    original = write_raster(tmp_path / "wide.tif", rgb(1000, 2000), photometric="RGB")

    with rasterio.open(str(render(Thumbnail(), original, tmp_path))) as png:
        assert (png.driver, png.width, png.height, png.count) == ("PNG", THUMBNAIL_PX, 256, 3)
        assert png.colorinterp == (ColorInterp.red, ColorInterp.green, ColorInterp.blue)


def test_a_thumbnail_stretches_deep_samples_and_hides_nodata_and_nan(tmp_path):
    ramp = np.tile(np.linspace(1000, 5000, 100, dtype=np.float32), (100, 1))
    ramp[:, :10] = 0  # nodata
    ramp[:10, 50:] = np.nan
    original = write_raster(tmp_path / "dem.tif", ramp[np.newaxis], nodata=0)

    with rasterio.open(str(render(Thumbnail(), original, tmp_path))) as png:
        grey, alpha = png.read()
    assert png.count == 2
    assert (alpha[:, :10] == 0).all() and (alpha[:10, 50:] == 0).all()
    assert (alpha[10:, 10:] == 255).all()
    shown = grey[alpha == 255]
    assert shown.min() <= 5 and shown.max() >= 250  # stretched across the range


def test_a_paletted_raster_shows_its_palette(tmp_path):
    indexes = np.zeros((1, 64, 64), dtype=np.uint8)
    indexes[:, :, 32:] = 1
    original = tmp_path / "classes.tif"
    write_raster(original, indexes, photometric="PALETTE")
    with rasterio.open(str(original), "r+") as dataset:
        dataset.write_colormap(1, {0: (200, 0, 0, 255), 1: (0, 0, 200, 255)})

    with rasterio.open(str(render(Thumbnail(), original, tmp_path))) as png:
        red, green, blue = png.read()
    assert (red[:, :32] == 200).all() and (blue[:, :32] == 0).all()
    assert (blue[:, 32:] == 200).all() and (red[:, 32:] == 0).all()
    assert (green == 0).all()


@pytest.mark.invariant("ING-7")
@pytest.mark.parametrize("recipe", RASTER, ids=lambda r: r.role)
def test_the_same_original_renders_the_same_bytes(tmp_path, recipe):
    original = write_raster(tmp_path / "ortho.tif", rgb(700, 900), nodata=0)

    first = render(recipe, original, tmp_path).read_bytes()
    second = render(recipe, original, tmp_path).read_bytes()

    check("ING-7", recipe.deterministic and first == second, f"{recipe.role} differs")


@pytest.mark.parametrize("recipe", RASTER, ids=lambda r: r.role)
def test_a_raster_gdal_cannot_read_is_unrenderable(tmp_path, recipe):
    original = tmp_path / "broken.tif"
    original.write_bytes(b"II*\x00" + bytes(range(256)) * 8)

    with pytest.raises(Unrenderable):
        render(recipe, original, tmp_path)


def test_renders_leave_nothing_beside_the_original(tmp_path):
    """No `.aux.xml` or temporary file outside each recipe's own scratch directory."""
    original = write_raster(tmp_path / "ortho.tif", rgb(600, 600))
    for recipe in RASTER:
        render(recipe, original, tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["ortho.tif"]
