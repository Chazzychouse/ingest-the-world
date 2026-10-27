"""A GLB's geometry, read from its local copy a chunk at a time: what the tiler reads models with.

Opening a model parses its JSON chunk and checks every primitive it will read before reading a
byte of geometry: each accessor a primitive uses must lie inside its buffer view, and each view
inside the binary chunk, with a stride and component type glTF allows. Rows are then read by
position (`read`, `gather`), converted to float64 (normalized integers scaled as glTF says) or,
for indices, to uint32, with every index checked against its vertex count. A model that fails
any check, or needs an extension the tiler can't honour (Draco, meshopt), is `Unrenderable`.

Only geometry stored in the GLB's own binary chunk is read; a buffer naming another file (a
`.gltf` bundle) is refused. Nodes are walked from the scene's roots, each at most once, so a
cycle or a shared child can't make the walk unbounded.
https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html
"""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

import numpy as np
from numpy.typing import NDArray

from ingest_api.worker.formats.glb import CHUNK, HEADER, JSON_CHUNK, MAGIC, TRIANGLES
from ingest_api.worker.formats.json_values import as_list, as_number, as_object, integer, objects
from ingest_api.worker.meshes.model import NO_MATERIAL
from ingest_api.worker.recipe import Unrenderable

BIN_CHUNK = 0x004E4942  # "BIN\0", little-endian
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_NODES = 1_000_000
MAX_PRIMITIVES = 100_000
MAX_STRIDE = 252  # the spec's limit on a buffer view's byteStride
# Extensions a model may require that the tiler honours: quantized attributes are read as
# glTF says, and material extensions don't change geometry (the tiles keep base colour only).
HONOURED = frozenset({"KHR_mesh_quantization"})
HONOURED_PREFIXES = ("KHR_materials_",)
COMPONENTS: dict[int, np.dtype[np.number]] = {
    5120: np.dtype(np.int8),
    5121: np.dtype(np.uint8),
    5122: np.dtype(np.int16),
    5123: np.dtype(np.uint16),
    5125: np.dtype(np.uint32),
    5126: np.dtype(np.float32),
}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
INDEX_TYPES = frozenset({5121, 5123, 5125})
IMAGE_TYPES = frozenset({"image/jpeg", "image/png"})

type Matrix = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class Accessor:
    """Where an accessor's rows are in the file, and how to decode them."""

    offset: int  # of its first row, from the start of the file
    stride: int
    dtype: np.dtype[np.number]
    width: int
    count: int
    normalized: bool

    def span(self, start: int, count: int) -> tuple[int, int]:
        """The file offset and byte length holding rows `start` to `start + count`."""
        return self.offset + start * self.stride, (count - 1) * self.stride + self.row_bytes

    @property
    def row_bytes(self) -> int:
        return self.dtype.itemsize * self.width


@dataclass(frozen=True, slots=True)
class Primitive:
    positions: Accessor
    normals: Accessor | None
    uvs: Accessor | None
    indices: Accessor | None
    material: int
    transform: Matrix  # 4x4, the node's world matrix

    @property
    def triangles(self) -> int:
        corners = self.indices.count if self.indices is not None else self.positions.count
        return corners // 3


@dataclass(frozen=True, slots=True)
class Material:
    base_colour: tuple[float, float, float, float]
    image: int | None  # its base colour texture's image, if any
    double_sided: bool


class Glb:
    """An open GLB. A context manager: it holds the file open until closed."""

    def __init__(self, path: Path):
        self._file = path.open("rb")
        try:
            self.document, self._binary, self._binary_length = _open(self._file.fileno())
            _check_extensions(self.document)
            self._accessors = objects(self.document, "accessors")
            self._views = objects(self.document, "bufferViews")
            self.primitives = list(_primitives(self))
            self.materials = [
                _material(m, self.document) for m in objects(self.document, "materials")
            ]
        except BaseException:
            self._file.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._file.close()

    def accessor(self, index: int | None, *, widths: frozenset[int]) -> Accessor | None:
        """Accessor `index`, checked to lie inside the binary chunk; None for no index."""
        if index is None:
            return None
        if not 0 <= index < len(self._accessors):
            raise Unrenderable(f"accessor {index} doesn't exist")
        found = _accessor(self._accessors[index], self, index)
        if found.width not in widths:
            raise Unrenderable(f"accessor {index} has {found.width} components")
        return found

    def view(self, index: int) -> tuple[int, int, int | None]:
        """Buffer view `index`'s file offset, length and stride, checked against the chunk."""
        if not 0 <= index < len(self._views):
            raise Unrenderable(f"buffer view {index} doesn't exist")
        view = self._views[index]
        if (integer(view, "buffer") or 0) != 0 or _uri(self.document, integer(view, "buffer")):
            raise Unrenderable("geometry outside the GLB's own binary chunk")
        offset, length = integer(view, "byteOffset") or 0, integer(view, "byteLength")
        if length is None or offset < 0 or length < 0 or offset + length > self._binary_length:
            raise Unrenderable(f"buffer view {index} lies outside the binary chunk")
        return self._binary + offset, length, integer(view, "byteStride")

    def read(self, accessor: Accessor, start: int, count: int) -> NDArray[np.float64]:
        """Rows `start` to `start + count` (clipped to the accessor), as float64."""
        return _decoded(self._rows(accessor, start, count), accessor)

    def read_indices(self, primitive: Primitive, start: int, count: int) -> NDArray[np.uint32]:
        """Triangles `start` to `start + count` of `primitive`, as rows of three vertex ids."""
        corners = max(0, min(count, primitive.triangles - start)) * 3
        if primitive.indices is None:
            first = start * 3
            return np.arange(first, first + corners, dtype=np.uint32).reshape(-1, 3)
        ids = self._rows(primitive.indices, start * 3, corners).reshape(-1).astype(np.uint32)
        if len(ids) and int(ids.max()) >= primitive.positions.count:
            raise Unrenderable(f"an index past the {primitive.positions.count} vertices it has")
        return ids.reshape(-1, 3)

    def gather(
        self, accessor: Accessor, ids: NDArray[np.uint32], *, gap: int
    ) -> NDArray[np.float64]:
        """The rows at `ids` (sorted, unique), reading each run of them, and gaps up to `gap` rows
        between, at once."""
        return _decoded(np.concatenate(list(self._runs(accessor, ids, gap))), accessor)

    def image(self, index: int) -> bytes | None:
        """Image `index`'s encoded bytes, if it's a JPEG or PNG stored in the binary chunk."""
        images = objects(self.document, "images")
        if not 0 <= index < len(images) or images[index].get("mimeType") not in IMAGE_TYPES:
            return None
        view = integer(images[index], "bufferView")
        if view is None:
            return None
        offset, length, _ = self.view(view)
        return os.pread(self._file.fileno(), length, offset)

    def _rows(self, accessor: Accessor, start: int, count: int) -> NDArray[np.number]:
        count = max(0, min(count, accessor.count - start))
        if count == 0:
            return np.zeros((0, accessor.width), accessor.dtype)
        offset, length = accessor.span(start, count)
        data = os.pread(self._file.fileno(), length, offset)
        if len(data) != length:
            raise Unrenderable("the file ends inside an accessor")
        shape, strides = (count, accessor.width), (accessor.stride, accessor.dtype.itemsize)
        return np.ndarray(shape, accessor.dtype, data, 0, strides).copy()

    def _runs(
        self, accessor: Accessor, ids: NDArray[np.uint32], gap: int
    ) -> Iterator[NDArray[np.number]]:
        if len(ids) == 0:
            yield np.zeros((0, accessor.width), accessor.dtype)
        for first, last in runs(ids, gap):
            low, high = int(ids[first]), int(ids[last - 1])
            block = self._rows(accessor, low, high - low + 1)
            yield block[ids[first:last].astype(np.int64) - low]


def runs(ids: NDArray[np.uint32], gap: int) -> list[tuple[int, int]]:
    """Ranges `[first, last)` of positions in `ids` (sorted) read as one block each: neighbours
    in a range are at most `gap` apart."""
    if len(ids) == 0:
        return []
    breaks = np.flatnonzero(np.diff(ids.astype(np.int64)) > gap) + 1
    starts = [0, *breaks.tolist()]
    return list(zip(starts, [*breaks.tolist(), len(ids)], strict=True))


def _open(fd: int) -> tuple[dict[str, object], int, int]:
    """The JSON chunk, and where the binary chunk starts and how long it is."""
    head = os.pread(fd, HEADER.size + CHUNK.size, 0)
    if len(head) < HEADER.size + CHUNK.size:
        raise Unrenderable("too short to be a GLB")
    magic, version, total = HEADER.unpack_from(head)
    json_length, json_type = CHUNK.unpack_from(head, HEADER.size)
    if magic != MAGIC or version != 2 or json_type != JSON_CHUNK:
        raise Unrenderable("not a glTF 2.0 binary")
    if json_length > MAX_JSON_BYTES:
        raise Unrenderable(f"a JSON chunk of {json_length} bytes")
    start = HEADER.size + CHUNK.size
    try:
        document = as_object(json.loads(os.pread(fd, json_length, start)))
    except (ValueError, RecursionError) as exc:
        raise Unrenderable(f"GLB JSON chunk: {exc}") from exc
    if document is None:
        raise Unrenderable("the GLB's JSON chunk is not an object")
    binary_at = start + json_length + (-json_length % 4)
    chunk = os.pread(fd, CHUNK.size, binary_at)
    if len(chunk) < CHUNK.size:
        return document, binary_at, 0  # no binary chunk: only empty accessors read
    length, kind = CHUNK.unpack(chunk)
    if kind != BIN_CHUNK or binary_at + CHUNK.size + length > total:
        raise Unrenderable("the GLB's binary chunk is malformed")
    return document, binary_at + CHUNK.size, length


def _check_extensions(document: dict[str, object]) -> None:
    for name in as_list(document.get("extensionsRequired")) or []:
        if not isinstance(name, str):
            continue
        if name not in HONOURED and not name.startswith(HONOURED_PREFIXES):
            raise Unrenderable(f"the model requires {name}")


def _uri(document: dict[str, object], buffer: int | None) -> bool:
    buffers = objects(document, "buffers")
    index = buffer or 0
    return 0 <= index < len(buffers) and "uri" in buffers[index]


def _accessor(raw: dict[str, object], glb: Glb, index: int) -> Accessor:
    component, count = integer(raw, "componentType"), integer(raw, "count")
    width = WIDTHS.get(str(raw.get("type")))
    if component not in COMPONENTS or width is None or count is None or count < 0:
        raise Unrenderable(f"accessor {index} is malformed")
    if "sparse" in raw:
        raise Unrenderable(f"accessor {index} is sparse")
    dtype = COMPONENTS[component]
    view = integer(raw, "bufferView")
    if view is None:
        raise Unrenderable(f"accessor {index} has no buffer view")
    start, length, stride = glb.view(view)
    row = dtype.itemsize * width
    stride = stride or row
    if stride < row or stride > MAX_STRIDE or stride % dtype.itemsize:
        raise Unrenderable(f"accessor {index} has a stride of {stride}")
    offset = integer(raw, "byteOffset") or 0
    if (
        offset < 0
        or offset % dtype.itemsize
        or (count and offset + (count - 1) * stride + row > length)
    ):
        raise Unrenderable(f"accessor {index} reaches past its buffer view")
    normalized = raw.get("normalized") is True
    return Accessor(start + offset, stride, dtype, width, count, normalized)


def _decoded(rows: NDArray[np.number], accessor: Accessor) -> NDArray[np.float64]:
    """Rows as float64, with normalized integers mapped to [0, 1] or [-1, 1] as glTF says."""
    values = rows.astype(np.float64)
    if accessor.normalized and accessor.dtype.kind in "iu":
        signed = accessor.dtype.kind == "i"
        values /= float(2 ** (8 * accessor.dtype.itemsize - signed) - 1)  # the type's largest
        np.maximum(values, -1.0, out=values)
    return values


def _primitives(glb: Glb) -> Iterator[Primitive]:
    meshes = objects(glb.document, "meshes")
    count = 0
    for mesh_index, transform in _placed_meshes(glb.document):
        if not 0 <= mesh_index < len(meshes):
            raise Unrenderable(f"a node places mesh {mesh_index}, which doesn't exist")
        for raw in objects(meshes[mesh_index], "primitives"):
            if (integer(raw, "mode") or TRIANGLES) != TRIANGLES:
                continue  # points and lines have no surface to tile
            count += 1
            if count > MAX_PRIMITIVES:
                raise Unrenderable(f"more than {MAX_PRIMITIVES} primitives")
            yield _primitive(glb, raw, transform)


def _primitive(glb: Glb, raw: dict[str, object], transform: Matrix) -> Primitive:
    attributes = as_object(raw.get("attributes")) or {}
    positions = glb.accessor(integer(attributes, "POSITION"), widths=frozenset({3}))
    if positions is None:
        raise Unrenderable("a triangle primitive without positions")
    indices = glb.accessor(integer(raw, "indices"), widths=frozenset({1}))
    if indices is not None and indices.dtype not in (COMPONENTS[c] for c in INDEX_TYPES):
        raise Unrenderable("indices that aren't unsigned integers")
    normals = glb.accessor(integer(attributes, "NORMAL"), widths=frozenset({3}))
    uvs = glb.accessor(integer(attributes, "TEXCOORD_0"), widths=frozenset({2}))
    for accessor in (normals, uvs):
        if accessor is not None and accessor.count != positions.count:
            raise Unrenderable("attributes of different lengths in one primitive")
    material = integer(raw, "material")
    return Primitive(
        positions, normals, uvs, indices, NO_MATERIAL if material is None else material, transform
    )


def _placed_meshes(document: dict[str, object]) -> Iterator[tuple[int, Matrix]]:
    """Each mesh a node of the scene places, with that node's world matrix; nodes are visited
    breadth first, each once."""
    nodes = objects(document, "nodes")
    if len(nodes) > MAX_NODES:
        raise Unrenderable(f"more than {MAX_NODES} nodes")
    frontier: list[tuple[int, Matrix]] = [(i, np.eye(4)) for i in _roots(document, len(nodes))]
    seen: set[int] = set()
    for _ in range(len(nodes) + 1):  # bounded: every node enters the frontier at most once
        if not frontier:
            return
        level, frontier = frontier, []
        for index, parent in level:
            if index in seen or not 0 <= index < len(nodes):
                continue
            seen.add(index)
            world = parent @ _local(nodes[index])
            if (mesh := integer(nodes[index], "mesh")) is not None:
                yield mesh, world
            children = [
                c for c in as_list(nodes[index].get("children")) or [] if isinstance(c, int)
            ]
            frontier += [(c, world) for c in children]


def _roots(document: dict[str, object], node_count: int) -> list[int]:
    scenes = objects(document, "scenes")
    chosen = integer(document, "scene") or 0
    if not scenes:  # no scene: every node is shown, as viewers do
        return list(range(node_count))
    if not 0 <= chosen < len(scenes):
        raise Unrenderable(f"scene {chosen} doesn't exist")
    return [n for n in as_list(scenes[chosen].get("nodes")) or [] if isinstance(n, int)]


def _local(node: dict[str, object]) -> Matrix:
    """A node's matrix: `matrix` (column-major), or translation, rotation and scale."""
    if (matrix := _numbers(node, "matrix", 16)) is not None:
        return np.array(matrix, dtype=np.float64).reshape(4, 4).T
    tx, ty, tz = _numbers(node, "translation", 3) or [0.0, 0.0, 0.0]
    x, y, z, w = _numbers(node, "rotation", 4) or [0.0, 0.0, 0.0, 1.0]
    sx, sy, sz = _numbers(node, "scale", 3) or [1.0, 1.0, 1.0]
    rotation = np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )
    local = np.eye(4)
    local[:3, :3] = rotation * np.array([sx, sy, sz])
    local[:3, 3] = [tx, ty, tz]
    return local


def _numbers(parent: dict[str, object], key: str, length: int) -> list[float] | None:
    values = [as_number(v) for v in as_list(parent.get(key)) or []]
    found = [v for v in values if v is not None]
    return found if len(found) == length == len(values) else None


def _material(raw: dict[str, object], document: dict[str, object]) -> Material:
    pbr = as_object(raw.get("pbrMetallicRoughness")) or {}
    colour = _numbers(pbr, "baseColorFactor", 4) or [1.0, 1.0, 1.0, 1.0]
    texture = integer(as_object(pbr.get("baseColorTexture")) or {}, "index")
    textures = objects(document, "textures")
    image = None
    if texture is not None and 0 <= texture < len(textures):
        image = integer(textures[texture], "source")
    r, g, b, a = (min(1.0, max(0.0, c)) for c in colour)
    return Material((r, g, b, a), image, double_sided=raw.get("doubleSided") is True)
