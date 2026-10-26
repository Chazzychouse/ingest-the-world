"""Header readers, one module per format family. Each takes a `BlobSource` and returns what
the header says (`extract.Found`), or raises `extract.Unreadable`."""

from collections.abc import Mapping

from ingest_api.worker.classify import Media
from ingest_api.worker.extract import Reader
from ingest_api.worker.formats import geojson, geopackage, glb, las, parquet, raster, shapefile

# Which reader handles each classified media type; anything else is `unsupported`.
READERS: Mapping[str, Reader] = {
    Media.TIFF: raster.read,
    Media.PNG: raster.read,
    Media.JPEG: raster.read,
    Media.GLB: glb.read,
    Media.LAS: las.read,
    Media.LAZ: las.read,
    Media.PARQUET: parquet.read,
    Media.GEOPACKAGE: geopackage.read,
    Media.SHAPEFILE: shapefile.read,
    Media.GEOJSON: geojson.read,
}
