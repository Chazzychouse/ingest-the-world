# Local stub for what the mesh normalizer reads with assimp-py, which ships no type information.
from typing import Any

Process_Triangulate: int
Process_PreTransformVertices: int
Process_JoinIdenticalVertices: int
Process_SortByPType: int
Process_FindInvalidData: int
Process_ValidateDataStructure: int

class Mesh:
    vertices: memoryview
    normals: memoryview | None
    indices: memoryview | None
    texcoords: list[memoryview] | None
    num_uv_components: list[int]
    material_index: int
    num_vertices: int

class Scene:
    meshes: list[Mesh]
    materials: list[dict[str, Any]]

def import_file(path: str, flags: int) -> Scene: ...
def __getattr__(name: str) -> Any: ...
