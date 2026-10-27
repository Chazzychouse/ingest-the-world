"""A mesh in memory: triangles grouped by material, each group with its own vertices.

A `Mesh` maps a material (its index in the source document, or `NO_MATERIAL`) to a `Part`.
Parts are what tiles hold, what clustering simplifies and what the GLB writer writes; positions
are in the model's world coordinates (Y up, as glTF's), after every node transform.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

NO_MATERIAL = -1

type Floats = NDArray[np.float32]
type Indices = NDArray[np.uint32]


@dataclass(frozen=True, slots=True)
class Part:
    positions: Floats  # (n, 3)
    normals: Floats | None  # (n, 3), unit length
    uvs: Floats | None  # (n, 2)
    triangles: Indices  # (m, 3), into this part's vertices


type Mesh = Mapping[int, Part]


def triangle_count(mesh: Mesh) -> int:
    return sum(len(part.triangles) for part in mesh.values())


def merge(meshes: Iterable[Mesh]) -> dict[int, Part]:
    """One mesh holding all of `meshes`' triangles, a part per material."""
    grouped: dict[int, list[Part]] = {}
    for mesh in meshes:
        for material, part in mesh.items():
            grouped.setdefault(material, []).append(part)
    return {material: _concatenate(parts) for material, parts in grouped.items()}


def _concatenate(parts: list[Part]) -> Part:
    if len(parts) == 1:
        return parts[0]
    offsets = np.cumsum([0] + [len(p.positions) for p in parts[:-1]], dtype=np.uint64)
    triangles = [p.triangles + np.uint32(o) for p, o in zip(parts, offsets, strict=True)]
    return Part(
        positions=np.concatenate([p.positions for p in parts]),
        normals=_all_or_none([p.normals for p in parts]),
        uvs=_all_or_none([p.uvs for p in parts]),
        triangles=np.concatenate(triangles).astype(np.uint32),
    )


def _all_or_none(arrays: list[Floats | None]) -> Floats | None:
    """An attribute survives a merge only if every part has it."""
    present = [a for a in arrays if a is not None]
    return np.concatenate(present) if len(present) == len(arrays) else None


def bounds(mesh: Mesh) -> tuple[NDArray[np.float64], NDArray[np.float64]] | None:
    """The smallest box holding every vertex a triangle uses, or None for no triangles."""
    used = [p.positions[np.unique(p.triangles)] for p in mesh.values() if len(p.triangles)]
    if not used:
        return None
    every = np.concatenate(used).astype(np.float64)
    return every.min(axis=0), every.max(axis=0)
