"""The vector recipe, run inline on files OGR writes here: what the GeoParquet holds, which
originals it applies to (judged from what the vector readers recorded), and that the same
original always renders the same bytes. An original is a single file, as uploaded: a Shapefile
arrives without its `.shx`, `.dbf` or `.prj`."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pyogrio.raw
import pytest
import shapely
from rasterio.crs import CRS

from ingest_api.items.kinds import Kind
from ingest_api.worker.classify import Media
from ingest_api.worker.recipe import Unrenderable
from ingest_api.worker.recipes.vector import GeoParquet
from tests.geoparquet import geo_metadata, valid_geoparquet
from tests.invariants import check
from tests.test_raster_recipes import render, source

UTM_13N = "EPSG:32613"
POINTS = [shapely.Point(500_000 + 10 * i, 4_400_000 + 5 * i) for i in range(25)]


def write_vector(path: Path, driver: str, *, crs: str | None = UTM_13N) -> Path:
    """Twenty-five points with a name and a number each, as one file at `path`."""
    pyogrio.raw.write(
        str(path),
        geometry=np.array([shapely.to_wkb(p) for p in POINTS], dtype=object),
        field_data=[np.array([f"site {i}" for i in range(25)]), np.arange(25, dtype=np.int32)],
        fields=["name", "n"],
        crs=crs,
        driver=driver,
        geometry_type="Point",
    )
    return path


def uploaded_shapefile(tmp_path: Path, *, crs: str | None = UTM_13N) -> Path:
    """A Shapefile's `.shp` alone, as an upload brings it."""
    written = tmp_path / "written"
    written.mkdir(exist_ok=True)
    shp = write_vector(written / "sites.shp", "ESRI Shapefile", crs=crs)
    original = tmp_path / "sites.shp"
    original.write_bytes(shp.read_bytes())
    return original


def _geojson(tmp_path: Path) -> Path:
    features = [
        {"type": "Feature", "properties": {"n": i}, "geometry": shapely.geometry.mapping(p)}
        for i, p in enumerate(shapely.transform(POINTS, lambda xy: xy / 1e5))
    ]
    path = tmp_path / "sites.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return path


def _crs(projjson: dict) -> CRS:
    return CRS.from_user_input(json.dumps(projjson))


def _geopackage(tmp_path: Path) -> Path:
    return write_vector(tmp_path / "a.gpkg", "GPKG")


def test_a_geopackage_becomes_geoparquet_with_its_features_and_crs(tmp_path):
    original = write_vector(tmp_path / "sites.gpkg", "GPKG")

    out = render(GeoParquet(), original, tmp_path)

    table = valid_geoparquet(out)
    geo = geo_metadata(out)
    column = geo["columns"]["geometry"]
    assert (geo["version"], geo["primary_column"], column["encoding"]) == (
        "1.1.0",
        "geometry",
        "WKB",
    )
    assert column["geometry_types"] == ["Point"]
    assert _crs(column["crs"]) == CRS.from_user_input(UTM_13N)
    assert table.column("name").to_pylist() == [f"site {i}" for i in range(25)]
    assert table.column("n").to_pylist() == list(range(25))
    assert shapely.from_wkb(table.column("geometry").to_pylist()).tolist() == POINTS


def test_a_shapefile_uploaded_alone_keeps_its_points_and_an_unknown_crs(tmp_path):
    original = uploaded_shapefile(tmp_path)

    out = render(GeoParquet(), original, tmp_path)

    table = valid_geoparquet(out)
    # Without its `.prj` the CRS is unknown: null, never left out (which would mean WGS84).
    assert geo_metadata(out)["columns"]["geometry"]["crs"] is None
    assert "crs" in geo_metadata(out)["columns"]["geometry"]
    assert shapely.from_wkb(table.column("geometry").to_pylist()).tolist() == POINTS


def test_a_geojson_becomes_geoparquet_in_wgs84(tmp_path):
    out = render(GeoParquet(), _geojson(tmp_path), tmp_path)

    crs = _crs(geo_metadata(out)["columns"]["geometry"]["crs"])
    assert crs.to_epsg() == 4326
    assert valid_geoparquet(out).column("n").to_pylist() == list(range(25))


def test_an_empty_layer_becomes_an_empty_geoparquet(tmp_path):
    original = tmp_path / "empty.geojson"
    original.write_text('{"type": "FeatureCollection", "features": []}')

    assert valid_geoparquet(render(GeoParquet(), original, tmp_path)).num_rows == 0


@pytest.mark.parametrize(
    ("make", "media"),
    [
        (_geopackage, Media.GEOPACKAGE),
        (uploaded_shapefile, Media.SHAPEFILE),
        (_geojson, Media.GEOJSON),
    ],
    ids=["geopackage", "shapefile", "geojson"],
)
def test_the_recipe_applies_to_vector_files_it_converts(tmp_path, make, media):
    found = source(make(tmp_path), Kind.VECTOR, media)

    assert found.extraction == "extracted"
    assert GeoParquet().applies(found)


def test_the_recipe_skips_what_is_not_a_readable_vector_it_converts(tmp_path):
    vector = source(_geojson(tmp_path), Kind.VECTOR, Media.GEOJSON)
    recipe = GeoParquet()

    assert not recipe.applies(replace(vector, media_type=Media.PARQUET)), "already GeoParquet"
    assert not recipe.applies(replace(vector, kind=Kind.TABULAR))
    assert not recipe.applies(replace(vector, extraction="unreadable")), "it may be broken"
    # Too big to read within the header budget is not broken: the derive stage reads it all.
    assert recipe.applies(replace(vector, extraction="over-budget"))


@pytest.mark.invariant("ING-7")
@pytest.mark.parametrize("name", ["gpkg", "shapefile", "geojson"])
def test_the_same_original_renders_the_same_bytes(tmp_path, name):
    original = {
        "gpkg": lambda: write_vector(tmp_path / "sites.gpkg", "GPKG"),
        "shapefile": lambda: uploaded_shapefile(tmp_path),
        "geojson": lambda: _geojson(tmp_path),
    }[name]()
    recipe = GeoParquet()

    first = render(recipe, original, tmp_path).read_bytes()
    second = render(recipe, original, tmp_path).read_bytes()

    check("ING-7", recipe.deterministic and first == second, f"{name} differs")


def test_a_file_ogr_cannot_read_is_unrenderable(tmp_path):
    original = tmp_path / "broken.geojson"
    original.write_text('{"type": "FeatureCollection", "features": [{"type": "Feat')

    with pytest.raises(Unrenderable):
        render(GeoParquet(), original, tmp_path)


def test_renders_leave_nothing_beside_the_original(tmp_path):
    """The Shapefile's link and rebuilt `.shx` stay in the recipe's own scratch directory."""
    original = uploaded_shapefile(tmp_path)
    render(GeoParquet(), original, tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["sites.shp"]
