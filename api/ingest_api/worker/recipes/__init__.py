"""The recipes the derive stage runs, in the order it runs them."""

from ingest_api.worker.recipe import Recipe
from ingest_api.worker.recipes.mesh import Tiles3D
from ingest_api.worker.recipes.raster import Cog, Thumbnail
from ingest_api.worker.recipes.vector import GeoParquet

RASTER: tuple[Recipe, ...] = (Cog(), Thumbnail())
VECTOR: tuple[Recipe, ...] = (GeoParquet(),)
MESH: tuple[Recipe, ...] = (Tiles3D(),)
RECIPES: tuple[Recipe, ...] = (*RASTER, *VECTOR, *MESH)
