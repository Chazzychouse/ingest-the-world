"""A mesh as 3D Tiles 1.1: leaves that hold its triangles exactly, under coarser tiles that stand
for them, each declaring how far it may be from the surface (ING-14).

The model is read four times, a chunk at a time, and never held whole:

1. **Bounds**: every position, after its node's transform.
2. **Cells**: each vertex's cell in a fine grid over the bounds, written to scratch; then a count
   of triangles per cell (a triangle belongs to its first corner's cell).
3. **Tree**: from the counts alone, boxes of the grid split in half along their long axes until
   each holds at most `leaf_triangles` (or is one cell thick).
4. **Partition**: each triangle appended to its leaf's file in scratch.

Tiles are then built children first. A leaf reads its triangles' vertices and is written as it
is. A parent merges its children's tiles and clusters them on a grid of its own size over
`cells_per_tile` (`clustering`); its error is that grid's cell diagonal plus the largest of its
children's, so every level is within its error of the original. A tile's bounding box is the
union of its content's and its children's. Each tile gets its own texture crop (`textures`).
Memory is the grid, one chunk, and one tile with its children: not the model.
"""

import itertools
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from ingest_api.worker.meshes import clustering
from ingest_api.worker.meshes.archive import Writer
from ingest_api.worker.meshes.gltf import Glb, Primitive, runs
from ingest_api.worker.meshes.model import Floats, Indices, Mesh, Part, bounds, merge
from ingest_api.worker.meshes.textures import Baked, Textures
from ingest_api.worker.meshes.writer import glb as write_glb
from ingest_api.worker.recipe import Unrenderable

type Vector = NDArray[np.float64]
type Box = tuple[Vector, Vector]  # low and high corners, Y up
type Cells = NDArray[np.int64]

TILESET = "tileset.json"
# The fine grid's size, from the model's: a cell for every few triangles, and never so few that a
# leaf can't be cut small.
TRIANGLES_PER_CELL = 4
MIN_GRID_CELLS = 4096
# A partitioned triangle in scratch: which primitive, and its three vertex ids.
ROW = np.dtype([("primitive", "<u4"), ("a", "<u4"), ("b", "<u4"), ("c", "<u4")])


@dataclass(frozen=True, slots=True)
class Limits:
    leaf_triangles: int = 200_000
    cells_per_tile: int = 256  # a parent's clustering grid, along its longest axis
    grid_cells: int = 8_000_000  # the fine grid the tree is cut from, at most
    grid_side: int = 1024  # and along any one axis
    chunk_rows: int = 1_000_000  # vertices or triangles read at once
    gap_rows: int = 64  # gaps a gather reads through rather than seek past
    buffer_bytes: int = 64 * 1024 * 1024  # partitioned triangles held before an append
    leaf_texels: int = 2048
    texels: int = 1024


@dataclass(slots=True)
class Node:
    low: Cells  # grid cell indices, inclusive
    high: Cells  # exclusive
    triangles: int
    children: list[int] = field(default_factory=list[int])


@dataclass(frozen=True, slots=True)
class Built:
    """A tile's tileset entry, and what its parent needs from it."""

    entry: dict[str, object]
    error: float
    box: Box | None


@dataclass(frozen=True, slots=True)
class Summary:
    tiles: int
    leaves: int
    triangles: int


def tile(model: Glb, scratch: Path, out: Writer, limits: Limits | None = None) -> Summary:
    """Writes `model`'s tileset into `out`: `tileset.json` and `tiles/{n}.glb`."""
    job = _Job(model, scratch, out, limits or Limits())
    root = job.run()
    tileset = {
        "asset": {"version": "1.1"},
        "geometricError": max(root.error, _diagonal(root.box)),
        "root": root.entry,
    }
    out.add(TILESET, json.dumps(tileset, separators=(",", ":")).encode())
    leaves = sum(1 for n in job.nodes if not n.children)
    return Summary(len(job.nodes), leaves, sum(p.triangles for p in job.primitives))


class _Job:
    """One model being tiled: what every pass shares."""

    def __init__(self, model: Glb, scratch: Path, out: Writer, limits: Limits):
        self.model, self.scratch, self.out, self.limits = model, scratch, out, limits
        self.primitives = [p for p in model.primitives if p.triangles]
        self.nodes: list[Node] = []
        self.textures = Textures(model.image, scratch)

    def run(self) -> Built:
        if not self.primitives:
            raise Unrenderable("the model has no triangles")
        low, high = self._bounds()
        triangles = sum(p.triangles for p in self.primitives)
        shape = _grid(low, high, self.limits, triangles=triangles)
        cell = np.maximum((high - low) / shape, 1e-12)
        columns = [self._cells(i, low, cell, shape) for i in range(len(self.primitives))]
        self.nodes = _tree(self._counts(columns, shape), self.limits)
        leaves = [i for i, n in enumerate(self.nodes) if not n.children]
        self._partition(columns, leaves, shape)
        return self._build(leaves)

    # -- 1. bounds -----------------------------------------------------------------------------

    def _bounds(self) -> Box:
        low, high = np.full(3, np.inf), np.full(3, -np.inf)
        for primitive in self.primitives:
            for world in self._positions(primitive):
                low = np.minimum(low, world.min(axis=0))
                high = np.maximum(high, world.max(axis=0))
        if not (np.all(np.isfinite(low)) and np.all(np.isfinite(high))):
            raise Unrenderable("positions that aren't finite numbers")
        return low, high

    def _positions(self, primitive: Primitive) -> Iterator[Vector]:
        step = self.limits.chunk_rows
        for start in range(0, primitive.positions.count, step):
            yield _world(self.model.read(primitive.positions, start, step), primitive)

    # -- 2. cells and counts -------------------------------------------------------------------

    def _cells(self, index: int, low: Vector, cell: Vector, shape: Cells) -> Path:
        """Each vertex's flat cell index, as an int32 column in scratch."""
        path = self.scratch / f"cells-{index}.i32"
        with path.open("wb") as column:
            for world in self._positions(self.primitives[index]):
                grid = np.clip(((world - low) / cell).astype(np.int64), 0, shape - 1)
                flat = np.ravel_multi_index((grid[:, 0], grid[:, 1], grid[:, 2]), _dims(shape))
                column.write(flat.astype(np.int32).tobytes())
        return path

    def _cells_of(self, column: Path, ids: Indices) -> Cells:
        """The cells of vertices `ids`, in the order given."""
        unique, inverse = np.unique(ids, return_inverse=True)
        found = np.empty(len(unique), np.int64)
        with column.open("rb") as file:
            for first, last in runs(unique, self.limits.gap_rows):
                low, high = int(unique[first]), int(unique[last - 1])
                data = os.pread(file.fileno(), (high - low + 1) * 4, low * 4)
                block = np.frombuffer(data, np.int32)
                found[first:last] = block[unique[first:last].astype(np.int64) - low]
        return found[inverse.reshape(-1)]

    def _triangles(self, primitive: Primitive) -> Iterator[Indices]:
        step = self.limits.chunk_rows
        for start in range(0, primitive.triangles, step):
            yield self.model.read_indices(primitive, start, step)

    def _counts(self, columns: list[Path], shape: Cells) -> NDArray[np.int64]:
        counts = np.zeros(int(np.prod(shape)), np.int64)
        for primitive, column in zip(self.primitives, columns, strict=True):
            for triangles in self._triangles(primitive):
                cells = self._cells_of(column, triangles[:, 0])
                counts += np.bincount(cells, minlength=counts.size)
        return counts.reshape(_dims(shape))

    # -- 4. partition --------------------------------------------------------------------------

    def _partition(self, columns: list[Path], leaves: list[int], shape: Cells) -> None:
        """Appends each triangle, as a `ROW`, to its leaf's file in scratch."""
        leaf_of = np.full(_dims(shape), -1, np.int32)
        for number, index in enumerate(leaves):
            (x0, y0, z0), (x1, y1, z1) = self.nodes[index].low, self.nodes[index].high
            leaf_of[x0:x1, y0:y1, z0:z1] = number
        flat = leaf_of.reshape(-1)
        held: dict[int, list[bytes]] = {}
        size = 0
        for p, (primitive, column) in enumerate(zip(self.primitives, columns, strict=True)):
            for triangles in self._triangles(primitive):
                leaf = flat[self._cells_of(column, triangles[:, 0])]
                order = np.argsort(leaf, kind="stable")
                rows = np.empty(len(order), ROW)
                rows["primitive"] = p
                rows["a"], rows["b"], rows["c"] = triangles[order].T
                for number, start, stop in _groups(leaf[order]):
                    held.setdefault(number, []).append(rows[start:stop].tobytes())
                size += rows.nbytes
                if size > self.limits.buffer_bytes:
                    _append(held, self.scratch)
                    size = 0
        _append(held, self.scratch)

    # -- building tiles ------------------------------------------------------------------------

    def _build(self, leaves: list[int]) -> Built:
        """Every tile, children before parents; the root's, which holds the rest."""
        numbers = {index: number for number, index in enumerate(leaves)}
        built: dict[int, Built] = {}
        for index in reversed(_preorder(self.nodes)):
            node = self.nodes[index]
            children = [built.pop(c) for c in node.children]
            if children:
                mesh, error = self._parent(node, children)
            else:
                mesh, error = self._leaf(self.scratch / f"leaf-{numbers[index]}.rows"), 0.0
            if index != 0:  # the root has no parent to read it back
                _save(mesh, self.scratch / f"tile-{index}.npz")
            built[index] = self._write(index, mesh, error, children)
        return built[0]

    def _leaf(self, path: Path) -> dict[int, Part]:
        rows = np.fromfile(path, ROW)
        path.unlink()
        parts: list[Mesh] = []
        for p in np.unique(rows["primitive"]).tolist():
            mine = rows[rows["primitive"] == p]
            triangles = np.stack([mine["a"], mine["b"], mine["c"]], axis=1).astype(np.uint32)
            primitive = self.primitives[int(p)]
            parts.append({primitive.material: self._gathered(primitive, triangles)})
        return merge(parts)

    def _gathered(self, primitive: Primitive, triangles: Indices) -> Part:
        ids, local = np.unique(triangles, return_inverse=True)
        ids = ids.astype(np.uint32)
        gap = self.limits.gap_rows
        positions = _world(self.model.gather(primitive.positions, ids, gap=gap), primitive)
        normals = None
        if primitive.normals is not None:
            inverse = np.linalg.inv(primitive.transform[:3, :3]).T
            raw = self.model.gather(primitive.normals, ids, gap=gap) @ inverse.T
            length = np.maximum(np.linalg.norm(raw, axis=1, keepdims=True), 1e-12)
            normals = (raw / length).astype(np.float32)
        uvs = None
        if primitive.uvs is not None:
            uvs = self.model.gather(primitive.uvs, ids, gap=gap).astype(np.float32)
        triangles = local.reshape(-1, 3).astype(np.uint32)
        return Part(positions.astype(np.float32), normals, uvs, triangles)

    def _parent(self, node: Node, children: list[Built]) -> tuple[dict[int, Part], float]:
        merged = merge(_load(self.scratch / f"tile-{c}.npz") for c in node.children)
        worst = max(c.error for c in children)
        box = _union([c.box for c in children])
        if box is None:
            return {}, worst
        cell = max(float((box[1] - box[0]).max()) / self.limits.cells_per_tile, 1e-9)
        return clustering.cluster(merged, box[0], cell), clustering.diagonal(cell) + worst

    def _write(self, index: int, mesh: Mesh, error: float, children: list[Built]) -> Built:
        own = bounds(mesh)
        box = _union([own, *(c.box for c in children)])
        entry: dict[str, object] = {"geometricError": error, "refine": "REPLACE"}
        if box is not None:
            entry["boundingVolume"] = {"box": _box(box)}
        if own is not None:
            path = f"tiles/{index}.glb"
            baked = self._baked(mesh, self.limits.texels if children else self.limits.leaf_texels)
            centre = (own[0] + own[1]) / 2
            materials = self.model.materials
            self.out.add(path, write_glb(mesh, centre=centre, materials=materials, baked=baked))
            entry["content"] = {"uri": path}
        if children:
            entry["children"] = [c.entry for c in children]
        return Built(entry, error, box)

    def _baked(self, mesh: Mesh, texels: int) -> dict[int, Baked]:
        materials = self.model.materials
        baked: dict[int, Baked] = {}
        for material, part in mesh.items():
            image = materials[material].image if 0 <= material < len(materials) else None
            if image is None or part.uvs is None or len(part.triangles) == 0:
                continue
            if (found := self.textures.bake(image, part.uvs, texels=texels)) is not None:
                baked[material] = found
        return baked


def _world(rows: Vector, primitive: Primitive) -> Vector:
    m = primitive.transform
    return rows @ m[:3, :3].T + m[:3, 3]


def _dims(shape: Cells) -> tuple[int, int, int]:
    x, y, z = (int(v) for v in shape)
    return x, y, z


def _grid(low: Vector, high: Vector, limits: Limits, *, triangles: int) -> Cells:
    """Cells per axis, as near cubes as the box allows: a cell for every few triangles, up to
    about `grid_cells` in all, so a small model makes a small grid."""
    extent = np.maximum(high - low, 1e-9)
    relative = np.maximum(extent / extent.max(), 1 / limits.grid_side)
    cells = min(limits.grid_cells, max(MIN_GRID_CELLS, triangles // TRIANGLES_PER_CELL))
    side = (cells / float(relative.prod())) ** (1 / 3)
    return np.clip(np.ceil(side * relative), 1, limits.grid_side).astype(np.int64)


# -- 3. the tree -------------------------------------------------------------------------------


def _tree(counts: NDArray[np.int64], limits: Limits) -> list[Node]:
    """Boxes of the grid, the first the whole of it, each split until small enough."""
    table = np.zeros(np.array(counts.shape) + 1, np.int64)
    table[1:, 1:, 1:] = counts.cumsum(0).cumsum(1).cumsum(2)
    whole = np.array(counts.shape, np.int64)
    origin = np.zeros(3, np.int64)
    nodes = [Node(origin, whole, _sum(table, origin, whole))]
    pending = [0]
    for _ in range(int(counts.size) * 2 + 1):  # bounded: each split makes smaller boxes
        if not pending:
            break
        index = pending.pop()
        for low, high in _halves(nodes[index], limits):
            if triangles := _sum(table, low, high):
                nodes.append(Node(low, high, triangles))
                nodes[index].children.append(len(nodes) - 1)
                pending.append(len(nodes) - 1)
    return nodes


def _halves(node: Node, limits: Limits) -> Iterator[tuple[Cells, Cells]]:
    """`node`'s box cut in half along every axis at least half its longest, when it's over its
    triangle budget and can be cut."""
    size = node.high - node.low
    longest = int(size.max())
    axes = [a for a in range(3) if size[a] >= 2 and size[a] * 2 >= longest]
    if node.triangles <= limits.leaf_triangles or not axes:
        return
    cuts = [
        [int(node.low[a]), int(node.low[a] + node.high[a]) // 2, int(node.high[a])]
        if a in axes
        else [int(node.low[a]), int(node.high[a])]
        for a in range(3)
    ]
    for x0, x1 in zip(cuts[0], cuts[0][1:], strict=False):
        for y0, y1 in zip(cuts[1], cuts[1][1:], strict=False):
            for z0, z1 in zip(cuts[2], cuts[2][1:], strict=False):
                yield np.array([x0, y0, z0], np.int64), np.array([x1, y1, z1], np.int64)


def _sum(table: NDArray[np.int64], low: Cells, high: Cells) -> int:
    """Triangles in cells `low` to `high`, from the summed-volume table."""
    (x0, y0, z0), (x1, y1, z1) = _dims(low), _dims(high)
    t = table
    total = t[x1, y1, z1] - t[x0, y1, z1] - t[x1, y0, z1] - t[x1, y1, z0]
    return int(total + t[x0, y0, z1] + t[x0, y1, z0] + t[x1, y0, z0] - t[x0, y0, z0])


def _preorder(nodes: list[Node]) -> list[int]:
    order: list[int] = []
    pending = [0]
    for _ in range(len(nodes)):  # bounded: each node once
        if not pending:
            break
        index = pending.pop()
        order.append(index)
        pending.extend(nodes[index].children)
    return order


# -- 4. partition, in scratch ------------------------------------------------------------------


def _groups(sorted_leaves: NDArray[np.int32]) -> Iterator[tuple[int, int, int]]:
    """(leaf, start, stop) for each run of one leaf in `sorted_leaves`."""
    cuts = [0, *(np.flatnonzero(np.diff(sorted_leaves)) + 1).tolist(), len(sorted_leaves)]
    for start, stop in itertools.pairwise(cuts):
        if stop > start:
            yield int(sorted_leaves[start]), start, stop


def _append(held: dict[int, list[bytes]], scratch: Path) -> None:
    for number, blocks in held.items():
        with (scratch / f"leaf-{number}.rows").open("ab") as file:
            file.writelines(blocks)
    held.clear()


# -- tiles' geometry -----------------------------------------------------------------------


def _union(boxes: list[Box | None]) -> Box | None:
    present = [b for b in boxes if b is not None]
    if not present:
        return None
    return np.min([b[0] for b in present], axis=0), np.max([b[1] for b in present], axis=0)


def _box(box: Box) -> list[float]:
    """A Y-up box as 3D Tiles' Z-up `box`: its centre, then three half-axes."""
    x, y, z = (float(v) for v in (box[0] + box[1]) / 2)
    hx, hy, hz = (float(v) for v in np.maximum((box[1] - box[0]) / 2, 1e-6))
    return [x, -z, y, hx, 0.0, 0.0, 0.0, hz, 0.0, 0.0, 0.0, hy]


def _diagonal(box: Box | None) -> float:
    return 0.0 if box is None else float(np.linalg.norm(box[1] - box[0]))


# -- a child's tile in scratch, until its parent reads it ---------------------------------------


def _save(mesh: Mesh, path: Path) -> None:
    arrays: dict[str, Floats | Indices] = {}
    for material, part in mesh.items():
        arrays[f"{material}.positions"] = part.positions
        arrays[f"{material}.triangles"] = part.triangles
        if part.normals is not None:
            arrays[f"{material}.normals"] = part.normals
        if part.uvs is not None:
            arrays[f"{material}.uvs"] = part.uvs
    with path.open("wb") as file:
        np.savez(file, allow_pickle=False, **arrays)


def _load(path: Path) -> dict[int, Part]:
    with np.load(path, allow_pickle=False) as data:
        files = set(data.files)
        materials = sorted({int(name.split(".", 1)[0]) for name in files})

        def optional(key: str) -> Floats | None:
            return data[key] if key in files else None

        mesh = {
            m: Part(
                data[f"{m}.positions"],
                optional(f"{m}.normals"),
                optional(f"{m}.uvs"),
                data[f"{m}.triangles"],
            )
            for m in materials
        }
    path.unlink()
    return mesh
