"""What a file is, as far as the pipeline routes it. The database's `catalog.kinds` is kept in
step with this enum by a test."""

from enum import StrEnum


class Kind(StrEnum):
    RASTER = "raster"
    VECTOR = "vector"
    POINTCLOUD = "pointcloud"
    MESH = "mesh"
    TABULAR = "tabular"
    DOCUMENT = "document"
    GENERIC = "generic"
