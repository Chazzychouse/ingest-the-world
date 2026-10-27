"""Reading a model's primitives a chunk at a time, in world coordinates: what the tiler and the
coarse model both read with."""

from collections.abc import Iterator

import numpy as np
from numpy.typing import NDArray

from ingest_api.worker.meshes.gltf import Glb, Primitive
from ingest_api.worker.meshes.model import Indices
from ingest_api.worker.recipe import Unrenderable

type Vector = NDArray[np.float64]
type Box = tuple[Vector, Vector]  # low and high corners, Y up


def world(rows: Vector, primitive: Primitive) -> Vector:
    """Positions moved by their primitive's node transform."""
    m = primitive.transform
    return rows @ m[:3, :3].T + m[:3, 3]


def positions(model: Glb, primitive: Primitive, rows: int) -> Iterator[Vector]:
    """A primitive's positions in world coordinates, `rows` at a time."""
    for start in range(0, primitive.positions.count, rows):
        yield world(model.read(primitive.positions, start, rows), primitive)


def triangles(model: Glb, primitive: Primitive, rows: int) -> Iterator[Indices]:
    """A primitive's triangles as vertex ids, `rows` at a time."""
    for start in range(0, primitive.triangles, rows):
        yield model.read_indices(primitive, start, rows)


def bounds(model: Glb, primitives: list[Primitive], rows: int) -> Box:
    """The box holding every position, read in full rather than taken from the accessors'
    `min` and `max`, which a file may get wrong."""
    low, high = np.full(3, np.inf), np.full(3, -np.inf)
    for primitive in primitives:
        for chunk in positions(model, primitive, rows):
            low = np.minimum(low, chunk.min(axis=0))
            high = np.maximum(high, chunk.max(axis=0))
    if not (np.all(np.isfinite(low)) and np.all(np.isfinite(high))):
        raise Unrenderable("positions that aren't finite numbers")
    return low, high
