"""The recipes the derive stage runs, in the order it runs them."""

from ingest_api.worker.recipe import Recipe
from ingest_api.worker.recipes.raster import Cog, Thumbnail

RECIPES: tuple[Recipe, ...] = (Cog(), Thumbnail())
