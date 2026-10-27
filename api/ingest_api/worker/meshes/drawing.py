"""Drawing a coarse model as a picture, with no GPU: a thumbnail of a mesh.

An orthographic view from above one corner, the model's bounding box fitted to the frame; a
depth buffer; each triangle flat, its colour its material's base colour times its texture at the
triangle's centre, lit by one light from over the viewer's shoulder. What isn't the model is
transparent. A few seconds for the tens of thousands of triangles a coarse model has.
"""

from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

from ingest_api.worker.meshes.gltf import Material
from ingest_api.worker.meshes.model import Mesh
from ingest_api.worker.meshes.writer import DEFAULT

type Image = NDArray[np.uint8]  # (4, height, width): red, green, blue, alpha
type Sampler = Callable[[NDArray[np.float32]], NDArray[np.float64]]  # uvs -> (n, 3) colours in 0-1

VIEW = np.array([1.0, 0.9, 1.2])  # where the viewer stands, from the model's centre (Y up)
LIGHT = np.array([0.6, 1.0, 0.4])
AMBIENT = 0.35
MARGIN = 0.06  # of the frame, around the model


def _basis() -> NDArray[np.float64]:
    """Rows: the view's right, up and towards-the-viewer axes, in model coordinates."""
    back = VIEW / np.linalg.norm(VIEW)
    right = np.cross([0.0, 1.0, 0.0], back)
    right /= np.linalg.norm(right)
    return np.stack([right, np.cross(back, right), back])


def draw(mesh: Mesh, materials: list[Material], samplers: dict[int, Sampler], size: int) -> Image:
    """The mesh drawn into a `size`-pixel square."""
    basis = _basis()
    every = np.concatenate([p.positions for p in mesh.values()]).astype(np.float64) @ basis.T
    low, high = every[:, :2].min(axis=0), every[:, :2].max(axis=0)
    scale = size * (1 - 2 * MARGIN) / max(float((high - low).max()), 1e-12)
    offset = (size - (high - low) * scale) / 2 - low * scale
    colour = np.zeros((3, size, size))
    depth = np.full((size, size), -np.inf)
    light = LIGHT / np.linalg.norm(LIGHT)
    for material, part in mesh.items():
        if len(part.triangles) == 0:
            continue
        seen = part.positions.astype(np.float64) @ basis.T
        corners = seen[part.triangles.astype(np.int64)]  # (m, 3 corners, xyz in view)
        shade = _shade(part.positions.astype(np.float64)[part.triangles.astype(np.int64)], light)
        base = np.array(_material(materials, material).base_colour[:3])
        tint = np.tile(base, (len(corners), 1))
        if part.uvs is not None and material in samplers:
            tint *= samplers[material](part.uvs[part.triangles.astype(np.int64)].mean(axis=1))
        pixels = corners[:, :, :2] * scale + offset
        for n in range(len(corners)):  # bounded: one pass per triangle
            _fill(pixels[n], corners[n, :, 2], tint[n] * shade[n], colour, depth)
    alpha = np.where(np.isfinite(depth), 255, 0).astype(np.uint8)
    rgb = np.clip(colour * 255, 0, 255).astype(np.uint8)
    return np.concatenate([rgb[:, ::-1], alpha[np.newaxis, ::-1]])  # rows from the top down


def _material(materials: list[Material], index: int) -> Material:
    return materials[index] if 0 <= index < len(materials) else DEFAULT


def _shade(corners: NDArray[np.float64], light: NDArray[np.float64]) -> NDArray[np.float64]:
    """Each triangle's brightness, two-sided: a face lit from either side shows."""
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    return AMBIENT + (1 - AMBIENT) * np.abs(normals @ light)


def _fill(
    xy: NDArray[np.float64],
    z: NDArray[np.float64],
    rgb: NDArray[np.float64],
    colour: NDArray[np.float64],
    depth: NDArray[np.float64],
) -> None:
    """One triangle into the buffers, where it's nearer than what's there."""
    size = depth.shape[0]
    x0, y0 = np.maximum(np.floor(xy.min(axis=0)).astype(int), 0)
    x1, y1 = np.minimum(np.ceil(xy.max(axis=0)).astype(int), size - 1)
    if x1 < x0 or y1 < y0:
        return
    ys, xs = np.mgrid[y0 : y1 + 1, x0 : x1 + 1]
    px, py = xs + 0.5, ys + 0.5
    (ax, ay), (bx, by), (cx, cy) = xy
    area = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
    if abs(area) < 1e-12:
        return
    w0 = ((bx - px) * (cy - py) - (cx - px) * (by - py)) / area
    w1 = ((cx - px) * (ay - py) - (ax - px) * (cy - py)) / area
    w2 = 1 - w0 - w1
    inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
    nearer = inside & ((w0 * z[0] + w1 * z[1] + w2 * z[2]) > depth[y0 : y1 + 1, x0 : x1 + 1])
    if not nearer.any():
        return
    region = depth[y0 : y1 + 1, x0 : x1 + 1]
    region[nearer] = (w0 * z[0] + w1 * z[1] + w2 * z[2])[nearer]
    for band in range(3):
        colour[band, y0 : y1 + 1, x0 : x1 + 1][nearer] = rgb[band]
