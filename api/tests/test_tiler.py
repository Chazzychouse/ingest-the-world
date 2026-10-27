"""The tiler over random models (ING-14): leaves hold every triangle once and exactly, content
stays inside its bounding volume, error falls toward the leaves, and each coarser tile is within
its error of the model. Small limits make even small models deep trees, read in many chunks."""

from pathlib import Path

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ingest_api.tilesets import Writer
from ingest_api.worker.meshes.gltf import Glb
from ingest_api.worker.meshes.model import Mesh
from ingest_api.worker.meshes.tiler import Limits, tile
from tests.invariants import check
from tests.meshes import (
    boxes,
    meshes,
    original_triangles,
    terrain,
    tiles,
    triangle_set,
    write_model,
)

SMALL = Limits(
    leaf_triangles=24,
    cells_per_tile=4,
    grid_cells=4096,
    grid_side=64,
    chunk_rows=7,
    gap_rows=2,
    buffer_bytes=64,
)
offsets = st.tuples(*[st.integers(-80_000, 80_000).map(lambda v: v / 8)] * 3)


def tiled(tmp_path: Path, mesh: Mesh, offset: tuple[float, float, float], limits: Limits = SMALL):
    model, archive = tmp_path / "model.glb", tmp_path / "model.3tz"
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    write_model(model, mesh, offset=offset)
    with Glb(model) as glb, Writer(archive) as out:
        tile(glb, work, out, limits)
    return tiles(archive, tmp_path)


def _nested(t):
    """A tile's box with its parent's, if it has both."""
    if t.parent is not None:
        for own in boxes(t.entry):
            for above in boxes(t.parent.entry):
                yield own, above


def _moved(mesh: Mesh, offset: tuple[float, float, float]) -> np.ndarray:
    return original_triangles(mesh) + np.array(offset)


@pytest.mark.invariant("ING-14")
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(mesh=meshes(), offset=offsets)
def test_leaves_hold_every_triangle_once_and_exactly(tmp_path, mesh, offset):
    found = tiled(tmp_path, mesh, offset)

    leaves = [t for t in found if "children" not in t.entry]
    held = triangle_set(np.concatenate([t.triangles for t in leaves]))
    check("ING-14", held == triangle_set(_moved(mesh, offset)), "leaves differ from the model")


@pytest.mark.invariant("ING-14")
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(mesh=meshes(), offset=offsets)
def test_every_tile_is_inside_nested_boxes_and_within_its_error(tmp_path, mesh, offset):
    found = tiled(tmp_path, mesh, offset)
    original = np.unique(_moved(mesh, offset).reshape(-1, 3), axis=0)

    for t in found:
        error = float(t.entry["geometricError"])  # type: ignore[arg-type]
        check("ING-14", any(boxes(t.entry)), f"a depth-{t.depth} tile has no bounding volume")
        for (low, high), (above_low, above_high) in _nested(t):
            held = np.all(low >= above_low - 1e-6) and np.all(high <= above_high + 1e-6)
            check("ING-14", held, f"a depth-{t.depth} tile's box reaches outside its parent's")
        for low, high in boxes(t.entry):
            slack = 1e-6 * max(1.0, float(np.abs(np.concatenate([low, high])).max()))
            inside = np.all((t.positions >= low - slack) & (t.positions <= high + slack))
            check("ING-14", inside, f"a depth-{t.depth} tile reaches outside its box")
        if len(t.positions):
            nearest = np.min(np.linalg.norm(t.positions[:, None] - original[None], axis=2), axis=1)
            check(
                "ING-14",
                nearest.max() <= error + 1e-6,
                f"a vertex {nearest.max()} from the model, error {error}",
            )


@pytest.mark.invariant("ING-14")
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(mesh=meshes(), offset=offsets)
def test_error_falls_strictly_to_zero_at_the_leaves(tmp_path, mesh, offset):
    found = tiled(tmp_path, mesh, offset)

    for t in found:
        error = float(t.entry["geometricError"])  # type: ignore[arg-type]
        if "children" not in t.entry:
            check("ING-14", error == 0, f"a leaf declares error {error}")
        if t.parent is not None:
            above = float(t.parent.entry["geometricError"])  # type: ignore[arg-type]
            check("ING-14", error < above, f"error {error} under a parent's {above}")


def test_a_big_model_makes_a_tree_whose_root_is_coarser(tmp_path):
    part = terrain(60)
    limits = Limits(leaf_triangles=500, cells_per_tile=8, grid_cells=4096, grid_side=64)

    found = tiled(tmp_path, {0: part}, (0, 0, 0), limits)

    root, leaves = found[0], [t for t in found if "children" not in t.entry]
    assert len(leaves) > 4
    assert 0 < len(root.triangles) < len(part.triangles) / 4
