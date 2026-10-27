"""Mesh derivatives, made from the original's local copy by our own tiler (`worker/meshes`).

- `tileset`: the model as 3D Tiles 1.1, packed in one `.3tz` archive (M3 plan D-M3-5, D-M3-6):
  leaves holding its triangles exactly, coarser tiles standing for them within their declared
  error (ING-14), each tile with its own crop of its textures.
- `proxy`: the whole model coarse (`meshes/coarse`), as one small GLB a viewer can load at once,
  its textures scaled to fit.
- `thumbnail`: that coarse model drawn (`meshes/drawing`), a PNG like a raster's thumbnail.

Only for GLBs whose header read cleanly and that have triangles to tile. The tiler holds a chunk
and a tile at a time, so a model of any size renders within the sandbox's memory limit; its
working files live in the recipe's scratch. Deterministic for a given numpy and GDAL (ING-7); a
change here that would change the output raises the version.
"""

from collections.abc import Mapping
from pathlib import Path

import numpy as np
import rasterio

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import TILESET_ROLE
from ingest_api.tilesets import Writer
from ingest_api.worker.classify import Media
from ingest_api.worker.meshes.coarse import coarse
from ingest_api.worker.meshes.drawing import Sampler, draw
from ingest_api.worker.meshes.gltf import Glb
from ingest_api.worker.meshes.textures import Textures, png
from ingest_api.worker.meshes.tiler import tile
from ingest_api.worker.meshes.writer import glb
from ingest_api.worker.recipe import Source

SOFTWARE: Mapping[str, str] = {"numpy": np.__version__, "GDAL": rasterio.__gdal_version__}
TRIANGLES = "mesh:triangles"
# The proxy: the model on a grid of 128 cells along its longest side (some tens of thousands of
# triangles for a surface), textures at most 1024 px. The thumbnail: coarser, as it's small.
# Each is coarsened further, if it must, to fit its triangle budget.
PROXY_CELLS, PROXY_TEXELS, PROXY_TRIANGLES = 128, 1024, 65_536
THUMBNAIL_CELLS, THUMBNAIL_PX, SAMPLED_PX, THUMBNAIL_TRIANGLES = 96, 512, 256, 32_768


class Tiles3D:
    role, version, media_type, suffix = TILESET_ROLE, 1, "application/zip", ".3tz"
    title, stac_roles = "3D Tiles (3tz archive)", ("data", "3d-tiles")
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        return _tileable(source)

    def render(self, original: Path, scratch: Path) -> Path:
        out, work = scratch / "tileset.3tz", scratch / "work"
        work.mkdir()
        with Glb(original) as model, Writer(out) as archive:
            tile(model, work, archive)
        return out


class Proxy:
    role, version, media_type, suffix = "proxy", 1, "model/gltf-binary", ".glb"
    title, stac_roles = "Proxy model", ("overview",)
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        return _tileable(source)

    def render(self, original: Path, scratch: Path) -> Path:
        out = scratch / "proxy.glb"
        with Glb(original) as model:
            mesh, (low, high) = coarse(model, cells=PROXY_CELLS, max_triangles=PROXY_TRIANGLES)
            textures = Textures(model.image, scratch)
            baked = {
                m: found
                for m, part in mesh.items()
                if part.uvs is not None
                and 0 <= m < len(model.materials)
                and (image := model.materials[m].image) is not None
                and (found := textures.bake(image, part.uvs, texels=PROXY_TEXELS)) is not None
            }
            data = glb(mesh, centre=(low + high) / 2, materials=model.materials, baked=baked)
        out.write_bytes(data)
        return out


class Thumbnail:
    role, version, media_type, suffix = "thumbnail", 1, "image/png", ".png"
    title, stac_roles = "Thumbnail", ("thumbnail",)
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        return _tileable(source)

    def render(self, original: Path, scratch: Path) -> Path:
        out = scratch / "thumbnail.png"
        with Glb(original) as model:
            mesh, _ = coarse(model, cells=THUMBNAIL_CELLS, max_triangles=THUMBNAIL_TRIANGLES)
            textures = Textures(model.image, scratch)
            samplers: dict[int, Sampler] = {}
            for m in mesh:
                image = model.materials[m].image if 0 <= m < len(model.materials) else None
                found = textures.sampler(image, pixels=SAMPLED_PX) if image is not None else None
                if found is not None:
                    samplers[m] = found
            picture = draw(mesh, model.materials, samplers, THUMBNAIL_PX)
        out.write_bytes(png(picture))
        return out


def _tileable(source: Source) -> bool:
    """A GLB whose header read cleanly, with triangles."""
    triangles = source.properties.get(TRIANGLES)
    return (
        source.kind == Kind.MESH
        and source.media_type == Media.GLB
        and source.extraction == "extracted"
        and isinstance(triangles, int)
        and triangles > 0
    )
