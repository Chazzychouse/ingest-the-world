"""Meshes in other formats, as a GLB the tiler reads: OBJ, FBX, PLY and STL through Assimp (M3
plan D-M3-3), in the render sandbox like everything else that decodes an original (ING-8).

Assimp reads the whole model into memory, so only originals up to `MAX_BYTES` are converted.
Its nodes' transforms are applied to the vertices, faces triangulated, and points and lines
left out. Each material keeps its diffuse colour; textures, which these formats keep in other
files the upload didn't bring, are left out. A model with no triangles is Unrenderable.

The original's local copy has no name of its own, and Assimp knows an OBJ or STL by its
extension: the format is sniffed from the bytes, and the copy linked under that extension.
"""

import struct
from pathlib import Path

import assimp_py
import numpy as np

from ingest_api.worker.formats.json_values import as_list, as_number
from ingest_api.worker.meshes.gltf import Material
from ingest_api.worker.meshes.model import NO_MATERIAL, Part, merge
from ingest_api.worker.meshes.writer import glb
from ingest_api.worker.recipe import Unrenderable

MAX_BYTES = 256 * 1024 * 1024
FLAGS = (
    assimp_py.Process_Triangulate
    | assimp_py.Process_PreTransformVertices
    | assimp_py.Process_JoinIdenticalVertices
    | assimp_py.Process_SortByPType
    | assimp_py.Process_FindInvalidData
    | assimp_py.Process_ValidateDataStructure
)
GLB_MAGIC, PLY_MAGIC, FBX_MAGIC = b"glTF", b"ply", b"Kaydara FBX Binary  \x00"
STL_HEADER, STL_TRIANGLE = 84, 50  # a binary STL: an 80-byte header, a count, 50 bytes a facet
DEFAULT_GREY = 0.8  # a material with no diffuse colour


def is_glb(path: Path) -> bool:
    with path.open("rb") as file:
        return file.read(4) == GLB_MAGIC


def to_glb(original: Path, scratch: Path) -> Path:
    """The model at `original` (not a GLB) as a GLB in `scratch`."""
    if original.stat().st_size > MAX_BYTES:
        raise Unrenderable(f"a model over {MAX_BYTES} bytes, which Assimp would read whole")
    named = scratch / f"original{_extension(original)}"
    named.symlink_to(original)
    try:
        scene = assimp_py.import_file(str(named), FLAGS)
    except RuntimeError as exc:  # assimp-py's one error: the file didn't import
        raise Unrenderable(f"Assimp: {exc}") from exc
    materials = [_material(m) for m in scene.materials]
    mesh: dict[int, list[Part]] = {}
    for found in scene.meshes:  # bounded: the scene's meshes
        part = _part(found)
        if part is not None:
            index = (
                found.material_index if 0 <= found.material_index < len(materials) else NO_MATERIAL
            )
            mesh.setdefault(index, []).append(part)
    if not mesh:
        raise Unrenderable("the model has no triangles")
    out = scratch / "normalized.glb"
    merged = {m: _join(parts) for m, parts in mesh.items()}
    centre = _centre(merged)
    out.write_bytes(glb(merged, centre=centre, materials=materials, baked={}))
    return out


def _extension(original: Path) -> str:
    with original.open("rb") as file:
        head = file.read(STL_HEADER)
    size = original.stat().st_size
    if head.startswith(FBX_MAGIC):
        return ".fbx"
    if head.startswith(PLY_MAGIC):
        return ".ply"
    if head.lstrip().startswith(b"solid") or _binary_stl(head, size):
        return ".stl"
    if head.lstrip().startswith(b"; FBX"):
        return ".fbx"
    return ".obj"


def _binary_stl(head: bytes, size: int) -> bool:
    if len(head) < STL_HEADER:
        return False
    (facets,) = struct.unpack_from("<I", head, 80)
    return size == STL_HEADER + STL_TRIANGLE * facets


def _part(mesh: assimp_py.Mesh) -> Part | None:
    """A mesh's triangles, or None if it has none (points and lines are sorted into their own)."""
    if mesh.indices is None or len(mesh.indices) < 3:
        return None
    positions = np.frombuffer(mesh.vertices, np.float32).reshape(-1, 3).copy()
    triangles = np.frombuffer(mesh.indices, np.uint32).reshape(-1, 3).copy()
    if len(triangles) == 0 or int(triangles.max()) >= len(positions):
        return None
    normals = None
    if mesh.normals is not None and len(mesh.normals) == positions.size:
        normals = np.frombuffer(mesh.normals, np.float32).reshape(-1, 3).copy()
    uvs = None
    if mesh.texcoords and mesh.num_uv_components and mesh.num_uv_components[0] == 2:
        uvs = np.frombuffer(mesh.texcoords[0], np.float32).reshape(-1, 2).copy()
        if len(uvs) != len(positions):
            uvs = None
    return Part(positions, normals, uvs, triangles)


def _join(parts: list[Part]) -> Part:
    return merge([{0: p} for p in parts])[0]


def _material(raw: dict[str, object]) -> Material:
    colour = [v for v in map(as_number, as_list(raw.get("COLOR_DIFFUSE")) or []) if v is not None]
    r, g, b = colour[:3] if len(colour) >= 3 else (DEFAULT_GREY, DEFAULT_GREY, DEFAULT_GREY)
    alpha = as_number(raw.get("OPACITY"))
    rgba = (r, g, b, 1.0 if alpha is None else alpha)
    r, g, b, a = (min(1.0, max(0.0, v)) for v in rgba)
    return Material((r, g, b, a), None, double_sided=True)


def _centre(mesh: dict[int, Part]) -> np.ndarray:
    every = np.concatenate([p.positions for p in mesh.values()]).astype(np.float64)
    return (every.min(axis=0) + every.max(axis=0)) / 2
