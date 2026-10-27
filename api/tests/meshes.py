"""Meshes for tests: random models written as GLBs, and tilesets read back out of `.3tz` archives.

Positions are multiples of 1/8 within a few thousand units, so every coordinate, and its
difference from any tile's centre, is exact in float32: what a tile holds can be compared with
the original exactly, not within a tolerance.
"""

import json
import os
import subprocess
import warnings
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import strategies as st
from numpy.typing import NDArray
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile

from ingest_api.worker.meshes.gltf import Glb, Material
from ingest_api.worker.meshes.model import Mesh, Part
from ingest_api.worker.meshes.textures import Baked
from ingest_api.worker.meshes.writer import glb

STEP = 8  # positions are multiples of 1/STEP


@st.composite
def parts(draw: st.DrawFn, *, max_vertices: int = 60, max_triangles: int = 120) -> Part:
    vertices = draw(st.integers(3, max_vertices))
    spread = draw(st.sampled_from([4, 64, 4000]))  # a small box, a medium one, a long one
    flat = draw(st.booleans())  # terrain-like: one axis much thinner
    rng = np.random.default_rng(draw(st.integers(0, 2**32 - 1)))
    grid = rng.integers(-spread * STEP, spread * STEP, (vertices, 3))
    if flat:
        grid[:, 1] //= 64
    triangles = rng.integers(0, vertices, (draw(st.integers(1, max_triangles)), 3))
    normals = rng.normal(size=(vertices, 3)) if draw(st.booleans()) else None
    uvs = rng.random((vertices, 2)) if draw(st.booleans()) else None
    return Part(
        (grid / STEP).astype(np.float32),
        None
        if normals is None
        else (normals / np.linalg.norm(normals, axis=1, keepdims=True)).astype(np.float32),
        None if uvs is None else uvs.astype(np.float32),
        triangles.astype(np.uint32),
    )


@st.composite
def meshes(draw: st.DrawFn) -> dict[int, Part]:
    materials = draw(st.integers(1, 3))
    return {m: draw(parts()) for m in range(materials)}


def write_model(path: Path, mesh: Mesh, *, offset: tuple[float, float, float] = (0, 0, 0)) -> Path:
    """`mesh` as a GLB whose node moves it by `offset` (a multiple of 1/STEP, so still exact)."""
    materials = [Material((0.5, 0.5, 0.5, 1.0), None, double_sided=False) for _ in mesh]
    moved = {m: _shifted(p, -np.array(offset)) for m, p in mesh.items()}
    path.write_bytes(glb(moved, centre=np.array(offset, np.float64), materials=materials, baked={}))
    return path


def _shifted(part: Part, by: NDArray[np.float64]) -> Part:
    positions = (part.positions.astype(np.float64) - by).astype(np.float32)
    return Part(positions, part.normals, part.uvs, part.triangles)


@dataclass(frozen=True)
class Tile:
    entry: dict[str, object]
    depth: int
    parent: "Tile | None"
    positions: NDArray[np.float64]  # every vertex its triangles use, in world coordinates
    triangles: NDArray[np.float64]  # (m, 3, 3): each triangle's corners
    normals: list[NDArray[np.float64]]  # each primitive's, as written


def tiles(archive: Path, tmp_path: Path) -> list[Tile]:
    """Every tile of the tileset in `archive`, parents before their children."""
    with zipfile.ZipFile(archive) as z:
        tileset = json.loads(z.read("tileset.json"))
        found: list[Tile] = []
        pending: list[tuple[dict[str, object], int, Tile | None]] = [(tileset["root"], 0, None)]
        for _ in range(100_000):
            if not pending:
                break
            entry, depth, parent = pending.pop(0)
            positions, triangles, normals = _content(z, entry, tmp_path)
            tile = Tile(entry, depth, parent, positions, triangles, normals)
            found.append(tile)
            pending += [(c, depth + 1, tile) for c in entry.get("children", [])]  # type: ignore[union-attr]  # pyright: ignore[reportUnknownVariableType] - JSON
    return found


def _content(
    z: zipfile.ZipFile, entry: dict[str, object], tmp_path: Path
) -> tuple[NDArray[np.float64], NDArray[np.float64], list[NDArray[np.float64]]]:
    """A tile's vertices, triangles and normals, from its `content` or each of its `contents`."""
    corners: list[NDArray[np.float64]] = []
    normals: list[NDArray[np.float64]] = []
    for content in _contents(entry):
        path = tmp_path / "tile.glb"
        path.write_bytes(z.read(str(content.get("uri"))))
        with Glb(path) as model:
            for p in model.primitives:
                rows = model.read(p.positions, 0, p.positions.count)
                world = rows @ p.transform[:3, :3].T + p.transform[:3, 3]
                ids = model.read_indices(p, 0, p.triangles)
                corners.append(world[ids.astype(np.int64)])
                if p.normals is not None:
                    normals.append(model.read(p.normals, 0, p.normals.count))
    triangles = np.concatenate(corners) if corners else np.zeros((0, 3, 3))
    return triangles.reshape(-1, 3), triangles, normals


def _contents(entry: dict[str, object]) -> list[dict[str, object]]:
    one, many = entry.get("content"), entry.get("contents")
    found: list[object] = [one] if isinstance(one, dict) else many if isinstance(many, list) else []  # pyright: ignore[reportUnknownVariableType] - JSON
    return [c for c in found if isinstance(c, dict)]  # pyright: ignore[reportUnknownVariableType] - JSON


def triangle_set(triangles: NDArray[np.float64]) -> list[tuple[float, ...]]:
    """Triangles as sorted tuples, each rotated to start at its least corner (keeping winding)."""
    keyed: list[tuple[float, ...]] = []
    for corners in triangles:
        rows = [tuple(c) for c in corners.tolist()]
        start = rows.index(min(rows))
        rotated = rows[start:] + rows[:start]
        keyed.append(tuple(v for row in rotated for v in row))
    return sorted(keyed)


def original_triangles(mesh: Mesh) -> NDArray[np.float64]:
    every = [p.positions.astype(np.float64)[p.triangles.astype(np.int64)] for p in mesh.values()]
    return np.concatenate(every)


def boxes(entry: dict[str, object]) -> Iterator[tuple[NDArray[np.float64], NDArray[np.float64]]]:
    """A tile's Z-up `box` as a Y-up low and high corner."""
    volume = entry.get("boundingVolume")
    if isinstance(volume, dict) and isinstance(box := volume.get("box"), list):
        x, ny, y, hx, _, _, _, hz, _, _, _, hy = (float(v) for v in box)  # pyright: ignore[reportUnknownVariableType] - JSON
        centre, half = np.array([x, y, -ny]), np.array([hx, hy, hz])
        yield centre - half, centre + half


VALIDATOR = Path(__file__).parents[1] / "tools/validators/node_modules/.bin/3d-tiles-validator"


def validated(tileset: Path, tmp_path: Path) -> dict[str, Any]:
    """Cesium's validator's report on `tileset` (a `.3tz`), and glTF-Validator's on every tile in
    it. Skips where the validator isn't installed (`make services` installs it), unless required."""
    if not VALIDATOR.exists():
        if os.environ.get("INGEST_REQUIRE_VALIDATORS"):
            raise FileNotFoundError(VALIDATOR)
        pytest.skip("3d-tiles-validator isn't installed; run `make services`")
    report = tmp_path / "validation.json"
    subprocess.run(  # noqa: S603 - our own pinned tool, on our own output
        [str(VALIDATOR), "--tilesetFile", str(tileset), "--reportFile", str(report)],
        capture_output=True,
        check=False,
        timeout=300,
    )
    return json.loads(report.read_text())


def terrain(side: int, *, seed: int = 5) -> Part:
    """A `side` x `side` grid of heights, textured edge to edge: a small model like S1's."""
    rng = np.random.default_rng(seed)
    x, z = np.meshgrid(np.arange(side), np.arange(side))
    heights = rng.integers(0, 64, (side, side)) / STEP
    positions = np.stack([x.ravel(), heights.ravel(), z.ravel()], axis=1).astype(np.float32)
    uvs = (np.stack([x.ravel(), z.ravel()], axis=1) / (side - 1)).astype(np.float32)
    corner = (np.arange(side - 1)[:, None] * side + np.arange(side - 1)[None]).ravel()
    triangles = np.concatenate(
        [
            np.stack([corner, corner + side, corner + 1], 1),
            np.stack([corner + 1, corner + side, corner + side + 1], 1),
        ]
    )
    return Part(positions, None, uvs, triangles.astype(np.uint32))


def textured_model(path: Path, *, side: int = 40, pixels: int = 256) -> Path:
    """`terrain(side)` with a `pixels`-square JPEG texture: the tiler's whole path, textures too."""
    rng = np.random.default_rng(9)
    image = rng.integers(0, 255, (3, pixels, pixels), dtype=np.uint8)
    with warnings.catch_warnings(), MemoryFile() as memory:
        warnings.simplefilter("ignore", NotGeoreferencedWarning)  # a texture has no place
        with memory.open(driver="JPEG", width=pixels, height=pixels, count=3, dtype="uint8") as out:
            out.write(image)
        jpeg = memory.read()
    part = terrain(side)
    assert part.uvs is not None
    material = Material((1.0, 1.0, 1.0, 1.0), 0, double_sided=False)
    baked = {0: Baked(jpeg, "image/jpeg", part.uvs)}
    path.write_bytes(glb({0: part}, centre=np.zeros(3), materials=[material], baked=baked))
    return path
