"""A whole model, coarse: every primitive clustered on one grid over its bounds as it is read, so
a proxy or a thumbnail costs one pass over the file and memory for the grid's occupied cells.

The streamed form of `clustering`: vertices are summed into their cell (per material, so
materials stay apart) a chunk at a time, and triangles kept as the cells of their corners, once
each, those whose corners fall in one cell dropped. A cell's vertex is the mean of everything
summed into it, within the cell's diagonal of each (`clustering.diagonal`).
"""

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from ingest_api.worker.meshes import clustering, streaming
from ingest_api.worker.meshes.gltf import Glb, Primitive
from ingest_api.worker.meshes.model import Indices, Part, triangle_count
from ingest_api.worker.recipe import Unrenderable

CHUNK_ROWS = 1_000_000
GAP_ROWS = 64
# Halvings of the grid a coarse model may need to fit its budget: 128 cells down to one.
MAX_COARSENINGS = 8
# Columns summed per vertex: position (3), normal (3), texture coordinates (2), and a count.
POSITION, NORMAL, UV, COUNT = slice(0, 3), slice(3, 6), slice(6, 8), 8
WIDTH = 9

type Keys = NDArray[np.int64]


@dataclass(slots=True)
class _Material:
    """One material's cells so far: their keys, sorted, and the sums of what fell in each; and
    its triangles, as rows of three cell keys."""

    keys: Keys = field(default_factory=lambda: np.zeros(0, np.int64))
    sums: NDArray[np.float64] = field(default_factory=lambda: np.zeros((0, WIDTH)))
    triangles: Keys = field(default_factory=lambda: np.zeros((0, 3), np.int64))
    normals: bool = True
    uvs: bool = True

    def add_vertices(self, keys: Keys, rows: NDArray[np.float64]) -> None:
        unique, inverse = np.unique(keys, return_inverse=True)
        sums = np.zeros((len(unique), WIDTH))
        np.add.at(sums, inverse.reshape(-1), rows)
        merged = np.union1d(self.keys, unique)
        total = np.zeros((len(merged), WIDTH))
        total[np.searchsorted(merged, self.keys)] += self.sums
        total[np.searchsorted(merged, unique)] += sums
        self.keys, self.sums = merged, total

    def add_triangles(self, corners: Keys) -> None:
        a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
        kept = corners[(a != b) & (b != c) & (a != c)]
        if len(kept) == 0:
            return
        turn = np.argmin(kept, axis=1)  # the smallest corner first, keeping the winding
        rows = np.arange(len(kept))
        rotated = np.stack([kept[rows, (turn + k) % 3] for k in range(3)], axis=1)
        self.triangles = np.unique(np.concatenate([self.triangles, rotated]), axis=0)

    def part(self) -> Part:
        used, renumbered = np.unique(self.triangles, return_inverse=True)
        sums = self.sums[np.searchsorted(self.keys, used)]
        mean = sums / sums[:, COUNT : COUNT + 1]
        normals = None
        if self.normals:
            raw = mean[:, NORMAL]
            normals = (raw / np.maximum(np.linalg.norm(raw, axis=1, keepdims=True), 1e-12)).astype(
                np.float32
            )
        uvs = mean[:, UV].astype(np.float32) if self.uvs else None
        triangles: Indices = renumbered.reshape(-1, 3).astype(np.uint32)
        return Part(mean[:, POSITION].astype(np.float32), normals, uvs, triangles)


def coarse(model: Glb, *, cells: int, max_triangles: int) -> tuple[dict[int, Part], streaming.Box]:
    """The model clustered on a grid of `cells` along its longest axis, coarser still until it
    has at most `max_triangles` (a volume, unlike a surface, fills its cells); and its bounds."""
    primitives = [p for p in model.primitives if p.triangles]
    if not primitives:
        raise Unrenderable("the model has no triangles")
    low, high = streaming.bounds(model, primitives, CHUNK_ROWS)
    cell = max(float((high - low).max()) / cells, 1e-9)
    materials: dict[int, _Material] = {}
    for primitive in primitives:
        found = materials.setdefault(primitive.material, _Material())
        found.normals &= primitive.normals is not None
        found.uvs &= primitive.uvs is not None
        _read(model, primitive, found, low, cell)
    mesh = {m: found.part() for m, found in materials.items() if len(found.triangles)}
    for _ in range(MAX_COARSENINGS):  # bounded: each halves the grid
        if triangle_count(mesh) <= max_triangles:
            break
        cell *= 2
        # Texture bands as coarse as the grid, so a texture's seams don't hold it back.
        bands = max(1, int(float((high - low).max()) / cell))
        mesh = clustering.cluster(mesh, low, cell, uv_bands=bands)
    return mesh, (low, high)


def _read(
    model: Glb, primitive: Primitive, into: _Material, low: NDArray[np.float64], cell: float
) -> None:
    """Sums `primitive`'s vertices into their cells, then adds its triangles' cells, each
    triangle's corners gathered again: memory is a chunk, not a column per vertex."""
    inverse_transpose = np.linalg.inv(primitive.transform[:3, :3])  # rows times M^-1 is M^-T
    for start in range(0, primitive.positions.count, CHUNK_ROWS):
        positions = streaming.world(model.read(primitive.positions, start, CHUNK_ROWS), primitive)
        rows = np.zeros((len(positions), WIDTH))
        rows[:, POSITION], rows[:, COUNT] = positions, 1.0
        if primitive.normals is not None:
            normals = model.read(primitive.normals, start, CHUNK_ROWS)
            rows[:, NORMAL] = normals @ inverse_transpose
        if primitive.uvs is not None:
            rows[:, UV] = model.read(primitive.uvs, start, CHUNK_ROWS)
        into.add_vertices(_keys(positions, low, cell), rows)
    for triangles in streaming.triangles(model, primitive, CHUNK_ROWS):
        ids, corners = np.unique(triangles, return_inverse=True)
        gathered = model.gather(primitive.positions, ids.astype(np.uint32), gap=GAP_ROWS)
        keys = _keys(streaming.world(gathered, primitive), low, cell)
        into.add_triangles(keys[corners.reshape(-1, 3)])


def _keys(positions: NDArray[np.float64], low: NDArray[np.float64], cell: float) -> Keys:
    q = np.floor((positions - low) / cell).astype(np.int64)
    return (q[:, 0] << 42) | (q[:, 1] << 21) | q[:, 2]
