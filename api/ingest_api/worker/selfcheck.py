"""`python -m ingest_api.worker --check`: proves an image can run the worker's native code.

Slice 4 shipped an image whose GDAL couldn't load (a missing system library), which only a
live upload revealed. This writes a tiny GeoTIFF with GDAL and a one-row Parquet file with
Arrow, extracts both through the process sandbox, as the worker would, and renders every recipe
from the GeoTIFF through the render sandbox, as the derive stage would. It touches no database,
storage or queue, so CI can run it on a freshly built image.
"""

import io
import logging
import tempfile
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from rasterio.transform import from_origin

from ingest_api.adapters.process_sandbox import Limits, ProcessRenderer, ProcessSandbox
from ingest_api.worker.classify import Media
from ingest_api.worker.extract import Budget, extract
from ingest_api.worker.formats import READERS
from ingest_api.worker.recipe import Unrenderable
from ingest_api.worker.recipes import RECIPES

log = logging.getLogger("ingest.worker")
LIMITS = Limits(timeout_seconds=30.0, memory_bytes=1024 * 1024 * 1024)
MAX_RENDER_BYTES = 64 * 1024 * 1024


def _geotiff() -> bytes:
    profile: dict[str, Any] = {
        "driver": "GTiff",
        "width": 8,
        "height": 8,
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": from_origin(-105.0, 40.0, 0.01, 0.01),
    }
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "check.tif"
        with rasterio.open(str(path), "w", **profile) as dataset:
            dataset.write(np.zeros((1, 8, 8), dtype="uint8"))
        return path.read_bytes()


def _parquet() -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.table({"reading": [1.0]}), buffer)
    return buffer.getvalue()


def _slice(data: bytes, offset: int, length: int) -> bytes:
    return data[offset : offset + length]


def _extracts(geotiff: bytes) -> bool:
    sandbox = ProcessSandbox(LIMITS)
    ok = True
    for media, data in ((Media.TIFF, geotiff), (Media.PARQUET, _parquet())):
        read = partial(_slice, data)
        budget = Budget(read, media, len(data))
        status = extract(READERS[media], budget, sandbox).metadata.status
        log.info("%s: %s", media, status)
        ok = ok and status == "extracted"
    return ok


def _renders(geotiff: bytes) -> bool:
    renderer = ProcessRenderer(LIMITS, max_file_bytes=MAX_RENDER_BYTES)
    ok = True
    with tempfile.TemporaryDirectory() as directory:
        original = Path(directory) / "original"
        original.write_bytes(geotiff)
        for recipe in RECIPES:
            scratch = Path(tempfile.mkdtemp(dir=directory))
            try:
                size = renderer.render(recipe, original, scratch).stat().st_size
            except Unrenderable as exc:
                log.error("%s: %s", recipe.role, exc)
                ok = False
                continue
            log.info("%s: %d bytes", recipe.role, size)
    return ok


def run() -> bool:
    """True if every sample extracts and every recipe renders; logs what failed otherwise."""
    log.info("GDAL %s, Arrow %s", rasterio.__gdal_version__, pa.__version__)
    geotiff = _geotiff()
    extracted = _extracts(geotiff)
    return _renders(geotiff) and extracted
