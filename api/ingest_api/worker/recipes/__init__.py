"""The recipes the derive stage runs, in the order it runs them. Raster recipes arrive in M2
slice 4; until then cataloged items are handed to the derive stage and need nothing."""

from ingest_api.worker.recipe import Recipe

RECIPES: tuple[Recipe, ...] = ()
