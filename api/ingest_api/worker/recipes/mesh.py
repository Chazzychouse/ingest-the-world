"""Mesh derivatives, made from the original's local copy by our own tiler (`worker/meshes`).

- `3dtiles`: the model as 3D Tiles 1.1, packed in one `.3tz` archive (M3 plan D-M3-5, D-M3-6):
  leaves holding its triangles exactly, coarser tiles standing for them within their declared
  error (ING-14), each tile with its own crop of its textures.

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
from ingest_api.worker.classify import Media
from ingest_api.worker.meshes.archive import Writer
from ingest_api.worker.meshes.gltf import Glb
from ingest_api.worker.meshes.tiler import tile
from ingest_api.worker.recipe import Source

SOFTWARE: Mapping[str, str] = {"numpy": np.__version__, "GDAL": rasterio.__gdal_version__}
TRIANGLES = "mesh:triangles"


class Tiles3D:
    role, version, media_type, suffix = "3dtiles", 1, "application/zip", ".3tz"
    title, stac_roles = "3D Tiles (3tz archive)", ("data", "3d-tiles")
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        triangles = source.properties.get(TRIANGLES)
        return (
            source.kind == Kind.MESH
            and source.media_type == Media.GLB
            and source.extraction == "extracted"
            and isinstance(triangles, int)
            and triangles > 0
        )

    def render(self, original: Path, scratch: Path) -> Path:
        out, work = scratch / "tileset.3tz", scratch / "work"
        work.mkdir()
        with Glb(original) as model, Writer(out) as archive:
            tile(model, work, archive)
        return out
