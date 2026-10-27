"""Simplifying a mesh by vertex clustering: every vertex in one cell of a grid becomes one.

The grid's cells are cubes of side `cell`. Vertices in a cell (and, so a texture seam stays a
seam, in one band of texture coordinates) merge into their mean, which lies inside that cell; a
triangle whose corners merge is dropped, as is a repeat of one already kept. Each merged vertex
is so within the cell's diagonal of every vertex it replaces, which is the error a tile built this
way declares (ING-14). Out-of-core simplification, after Lindstrom (2000): memory is the mesh
being simplified, and the result is the same for the same input.
"""

import math

import numpy as np
from numpy.typing import NDArray

from ingest_api.worker.meshes.model import Floats, Indices, Mesh, Part

# Texture coordinates are bucketed at this resolution in each cluster's key: vertices far apart
# in the texture (two sides of a seam) stay apart even when they're in the same cell.
UV_BANDS = 256
# Bits per axis in a cluster's key; a grid finer than this along any axis wraps, so the caller
# keeps cells per axis well under it.
KEY_BITS = 21


def diagonal(cell: float) -> float:
    """How far a merged vertex can be from one it replaced."""
    return cell * math.sqrt(3)


def cluster(
    mesh: Mesh, origin: NDArray[np.float64], cell: float, *, uv_bands: int = UV_BANDS
) -> dict[int, Part]:
    """`mesh` simplified on a grid of cubes of side `cell` whose corner is at `origin`, texture
    coordinates kept apart in `uv_bands` bands across the texture."""
    return {material: _part(part, origin, cell, uv_bands) for material, part in mesh.items()}


def _part(part: Part, origin: NDArray[np.float64], cell: float, uv_bands: int) -> Part:
    keys = _keys(part, origin, cell, uv_bands)
    _, first, members = np.unique(keys, return_index=True, return_inverse=True)
    clusters = len(first)
    triangles = _surviving(members.reshape(-1)[part.triangles].astype(np.uint32))
    used, renumbered = np.unique(triangles, return_inverse=True)
    members = members.reshape(-1)
    normals = None if part.normals is None else _mean(part.normals, members, clusters)
    if normals is not None:
        normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    uvs = None if part.uvs is None else _mean(part.uvs, members, clusters)
    positions = _mean(part.positions, members, clusters)
    return Part(
        positions=positions[used],
        normals=normals[used] if normals is not None else None,
        uvs=uvs[used] if uvs is not None else None,
        triangles=renumbered.reshape(-1, 3).astype(np.uint32),
    )


def _keys(part: Part, origin: NDArray[np.float64], cell: float, uv_bands: int) -> NDArray[np.int64]:
    q = np.floor((part.positions.astype(np.float64) - origin) / cell).astype(np.int64)
    q &= (1 << KEY_BITS) - 1
    keys = (q[:, 0] << (2 * KEY_BITS)) | (q[:, 1] << KEY_BITS) | q[:, 2]
    if part.uvs is None:
        return keys
    bands = np.floor(part.uvs.astype(np.float64) * uv_bands).astype(np.int64)
    uv_key = (bands[:, 0] & 0xFFFF) << 16 | (bands[:, 1] & 0xFFFF)
    # Combined into one sortable pair: cell first, then texture band.
    return np.unique(np.stack([keys, uv_key], axis=1), axis=0, return_inverse=True)[1].reshape(-1)


def _mean(values: Floats, members: NDArray[np.intp], clusters: int) -> Floats:
    """Each cluster's mean of `values`, by cluster number."""
    counts = np.bincount(members, minlength=clusters).astype(np.float64)
    sums = [np.bincount(members, values[:, axis], clusters) for axis in range(values.shape[1])]
    return (np.stack(sums, axis=1) / counts[:, np.newaxis]).astype(np.float32)


def _surviving(triangles: Indices) -> Indices:
    """Triangles whose corners stayed distinct, each once, in a canonical rotation."""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    kept = triangles[(a != b) & (b != c) & (a != c)]
    if len(kept) == 0:
        return kept.reshape(0, 3)
    turn = np.argmin(kept, axis=1)  # rotate the smallest index first, keeping the winding
    rows = np.arange(len(kept))
    rotated = np.stack([kept[rows, (turn + k) % 3] for k in range(3)], axis=1)
    return np.unique(rotated, axis=0).astype(np.uint32)
