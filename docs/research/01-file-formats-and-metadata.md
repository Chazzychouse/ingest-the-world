# 01 — File Formats and Metadata Extraction

Research for **ingest-the-world**: an internal geospatial ingestion platform on Azure Government. Files range from byte-sized IoT and lab readings to multi-GB GLB models.

Researched 2026-10-24 from primary sources only: specs, standards bodies, library docs and library source. Every claim carries a link. Statements marked **[uncertain]** are from domain knowledge and were not confirmed in a primary source during this pass. Verify them before relying on them.

---

## TL;DR

1. **Sniff by content, not by name.** Read the first ~4 KB plus the last ~64 KB (the zip central directory and Parquet footer are at the end). Match magic bytes first. Use the extension only as a tiebreaker. Zips and directories (KMZ, SLPK, USDZ, zipped Shapefile, `.gdb`) need a second pass that reads the archive's listing.
2. **Almost every large geospatial format can be described from headers alone, using range reads.** GDAL `/vsiaz/` with `/vsizip/` chaining does this for raster and vector. Point clouds (LAS/COPC header), GLB (JSON chunk), Parquet (footer), FlatGeobuf (header) and Zarr (JSON metadata) need a few kilobytes. Only text formats have to be streamed in full: GeoJSON, KML, OBJ, CSV, IFC (beyond its header), FASTA/FASTQ. So do FBX and stats-on-all-points.
3. **Use STAC 1.1 Items as the normalized record.** `geometry: null` is legal, so non-spatial bio and IoT files fit too. Add a project-specific extension for domain fields. Don't create one Item per tiny IoT reading: batch readings into time-partitioned assets, or point to a SensorThings-style time-series store.
4. **Convert on ingest, keeping the original:** COG (raster), COPC (point cloud), GeoParquet (vector and tabular), Zarr (multidimensional), 3D Tiles (mesh). Store the original as the canonical asset and the cloud-native copy as an alternate asset.
5. **Main library is GDAL (C++), called from Python** through rasterio, pyogrio and the `osgeo` bindings, together with PDAL, pyproj, xarray and pystac. Use TypeScript only for glTF (gltf-transform) and maybe the UI. On Azure Government, GDAL needs an explicit endpoint override because its default is `*.core.windows.net`.

---

## 1. Format inventory

Column key:
- **Detect** is how to identify the format reliably.
- **Metadata** is what can be extracted.
- **Lib** is the recommended extractor.
- **Header-only?** says whether metadata can come from range reads without downloading the whole file.

### 1.1 Raster

| Format | Detect | Metadata | Lib | Header-only? |
|---|---|---|---|---|
| **GeoTIFF** | TIFF magic `II*\0` / `MM\0*`. BigTIFF is `II+\0` / `MM\0+`. GeoTIFF adds GeoKey tag 34735. Sources: [TIFF 6.0](https://www.itu.int/itudoc/itu-t/com16/tiff-fx/docs/tiff6.pdf), [OGC GeoTIFF 1.1](https://docs.ogc.org/is/19-008r4/19-008r4.html), [BigTIFF](https://libtiff.gitlab.io/libtiff/specification/bigtiff.html) | CRS (GeoKeys), geotransform giving bbox and resolution, size, band count, dtype, nodata, overviews, compression, tags ([GDAL GTiff](https://gdal.org/en/stable/drivers/raster/gtiff.html)) | rasterio / `gdal.Info` ([rasterio](https://rasterio.readthedocs.io/en/stable/topics/reading.html)) | **Yes.** IFDs are parsed through `/vsiaz/` range reads ([GDAL VSI](https://gdal.org/en/stable/user/virtual_file_systems.html)). In a non-COG TIFF the IFD can sit anywhere, which costs more requests but still works. Exact stats need a full read; use overviews or approximate stats instead |
| **COG** | A GeoTIFF whose IFDs and overviews come first, with tiles. GDAL-written COGs carry a `LAYOUT=COG` "ghost area" header. Validate with `rio cogeo validate` ([GDAL COG](https://gdal.org/en/stable/drivers/raster/cog.html), [OGC COG standard](https://docs.ogc.org/is/21-026/21-026.html), [rio-cogeo](https://cogeotiff.github.io/rio-cogeo/)) | Same as GeoTIFF | rasterio | **Yes, cheaply.** Typically one or two requests ([cogeo.org](https://cogeo.org/)) |
| **JPEG 2000 / GMLJP2** | Signature box `00 00 00 0C 6A 50 20 20 0D 0A 87 0A`. A raw codestream starts `FF 4F FF 51` ([JPEG 2000 Part 1 / ITU T.800](https://www.itu.int/rec/T-REC-T.800)). Georeferencing comes from a GeoJP2 UUID box or a GMLJP2 XML box ([OGC GMLJP2](https://www.ogc.org/standards/gmljp2/)) | CRS, bbox, bands, resolution levels | GDAL JP2OpenJPEG driver ([docs](https://gdal.org/en/stable/drivers/raster/jp2openjpeg.html)) | **Yes** for metadata (header boxes). Decoding is slow; convert to COG |
| **NITF / NSIF** | ASCII `NITF02.10` / `NITF02.00` / `NSIF01.00` at offset 0 ([GDAL NITF](https://gdal.org/en/stable/drivers/raster/nitf.html), [NITFS / MIL-STD-2500C](https://nsgreg.nga.mil/doc/view?i=5546) **[uncertain: exact NSG registry doc id]**) | Multiple image segments become GDAL subdatasets. IGEOLO corner coordinates, TREs (RPC00B, BLOCKA, etc.), classification fields, acquisition time | GDAL NITF driver | **Yes.** Fixed-layout text headers at the front. **Classification fields (FSCLAS, etc.) should drive access control**, so treat them as security-relevant metadata |
| **MrSID** | Magic `msid` **[uncertain]** | CRS, bbox, bands | GDAL MrSID driver. **Needs the proprietary LizardTech/Extensis DSDK, which standard GDAL builds don't include** ([GDAL MrSID](https://gdal.org/en/stable/drivers/raster/mrsid.html)) | Yes, if the SDK is present. Plan to convert to COG in an isolated worker that has the SDK |
| **ECW** | Extension plus GDAL identification. No widely documented magic **[uncertain]** | CRS, bbox, bands | GDAL ECW driver. **Needs the proprietary ERDAS ECW/JP2 SDK** ([GDAL ECW](https://gdal.org/en/stable/drivers/raster/ecw.html)) | Same as MrSID |
| Other rasters likely seen: DTED, ERDAS `.img`, ESRI Grid, JPEG/PNG plus `.wld`/`.aux.xml` | GDAL identifies these with `gdal.IdentifyDriverEx` ([GDAL Python API](https://gdal.org/en/stable/api/python/raster_api.html)) | The sidecar files carry the georeferencing: [world file](https://gdal.org/en/stable/drivers/raster/wld.html), [PAM .aux.xml](https://gdal.org/en/stable/user/raster_data_model.html) | GDAL | Mostly yes |

### 1.2 Vector

| Format | Detect | Metadata | Lib | Header-only? |
|---|---|---|---|---|
| **Shapefile** (multi-file) | `.shp` starts with file code 9994 (`00 00 27 0A`, big-endian). The 100-byte header holds the shape type and bbox. **Requires** `.shx` and `.dbf`. `.prj` (WKT CRS), `.cpg` (encoding) and others are optional ([ESRI Shapefile whitepaper](https://www.esri.com/content/dam/esrisites/sitecore-archive/Files/Pdfs/library/whitepapers/pdfs/shapefile.pdf)) | bbox (from the .shp header), geometry type, feature count (from the .dbf header), fields, CRS (from .prj, which may be missing) | pyogrio ([docs](https://pyogrio.readthedocs.io/en/latest/introduction.html)) / GDAL ([driver](https://gdal.org/en/stable/drivers/vector/shapefile.html)) | **Yes.** **Bundle handling:** group siblings by basename, or accept a `.zip` and open it with `/vsizip/` |
| **GeoPackage** | SQLite header `SQLite format 3\0`, and `application_id` at offset 68 = `0x47504B47` ("GPKG") ([OGC GeoPackage 1.4](https://www.geopackage.org/spec/), [SQLite file format](https://www.sqlite.org/fileformat.html)) | Layers (`gpkg_contents`, including the declared min/max bbox), CRS (`gpkg_spatial_ref_sys`), geometry types, feature counts, raster tile pyramids, `last_change` timestamp | pyogrio / GDAL ([GPKG driver](https://gdal.org/en/stable/drivers/vector/gpkg.html)) | **Partly.** SQLite pages can be read over `/vsicurl/` but access is random and chatty. **[uncertain: performance]** For files over ~1 GB, download to local scratch first |
| **GeoJSON / GeoJSONSeq** | Text. JSON with `"type":"FeatureCollection"`/`"Feature"`. GeoJSON Text Sequences (RFC 8142) are RS-delimited or newline-delimited ([RFC 7946](https://datatracker.ietf.org/doc/html/rfc7946), [RFC 8142](https://datatracker.ietf.org/doc/html/rfc8142)) | bbox (often has to be computed), geometry types, properties schema. CRS is WGS84 by RFC 7946 | pyogrio / GDAL ([GeoJSON](https://gdal.org/en/stable/drivers/vector/geojson.html), [GeoJSONSeq](https://gdal.org/en/stable/drivers/vector/geojsonseq.html)) | **No.** Requires a full streaming parse |
| **KML / KMZ** | XML in the `http://www.opengis.net/kml/2.2` namespace. A KMZ is a zip whose root holds `doc.kml` ([OGC KML 2.3](https://docs.ogc.org/is/12-007r2/12-007r2.html)) | Placemarks, bbox (computed), `TimeStamp`/`TimeSpan`, embedded overlays | GDAL LIBKML driver ([docs](https://gdal.org/en/stable/drivers/vector/libkml.html)) | **No.** Full parse. KMZ needs a zip listing first |
| **FlatGeobuf** | Magic `66 67 62 03 66 67 62 xx`. Byte 4 is the major version ([FlatGeobuf](https://github.com/flatgeobuf/flatgeobuf)) | Header FlatBuffer: envelope, geometry type, columns, CRS (org, code, WKT), `features_count`, index node size ([header.fbs](https://github.com/flatgeobuf/flatgeobuf/blob/master/src/fbs/header.fbs)) | pyogrio / GDAL ([FGB driver](https://gdal.org/en/stable/drivers/vector/flatgeobuf.html)); `flatgeobuf` npm for TypeScript | **Yes.** Header plus an optional packed Hilbert R-tree, built for HTTP range reads |
| **GeoParquet** | Parquet magic `PAR1` at start and end. Footer = metadata + 4-byte length + `PAR1` ([Parquet format](https://parquet.apache.org/docs/file-format/)). Geo metadata sits in the `geo` key-value entry ([GeoParquet 1.1](https://geoparquet.org/releases/v1.1.0/)) | `geo.columns.*`: encoding, `geometry_types`, `crs` (PROJJSON, default OGC:CRS84), `bbox`, `covering`, `edges`, `epoch`. Row count and column schema come from the Parquet footer | pyarrow / geopandas / GDAL ([Parquet driver](https://gdal.org/en/stable/drivers/vector/parquet.html)); DuckDB spatial | **Yes.** Two range reads (tail length, then footer). **Note:** GeoParquet 2.0 (rc on [releases](https://geoparquet.org/releases/)) moves to the native Parquet `GEOMETRY`/`GEOGRAPHY` logical types added in Parquet format 2.11, which carry CRS and bbox statistics in the Parquet schema itself ([GeoParquet main spec](https://github.com/opengeospatial/geoparquet/blob/main/format-specs/geoparquet.md), [Parquet LogicalTypes](https://github.com/apache/parquet-format/blob/master/LogicalTypes.md)). The extractor must handle both |
| **Esri File Geodatabase** (`.gdb` directory) | A directory named `*.gdb` containing `a00000001.gdbtable`, etc. Usually arrives zipped | Feature classes, CRS, extents, domains, feature counts | GDAL OpenFileGDB driver (open source, no Esri SDK; write support since 3.6) ([docs](https://gdal.org/en/stable/drivers/vector/openfilegdb.html)) | **Yes**, through `/vsizip//vsiaz/...zip/x.gdb` |
| **CSV with coordinates** | Text. Look for lat/lon/WKT column names | Geometry built from columns. `X_POSSIBLE_NAMES` / `Y_POSSIBLE_NAMES` / `GEOM_POSSIBLE_NAMES` open options ([GDAL CSV](https://gdal.org/en/stable/drivers/vector/csv.html)) | GDAL / pandas | Header row only for the schema. bbox needs a full scan |

### 1.3 Point cloud

| Format | Detect | Metadata | Lib | Header-only? |
|---|---|---|---|---|
| **LAS** | `LASF` at offset 0. Public header block is 375 bytes in LAS 1.4 ([ASPRS LAS 1.4 (ASPRSorg/LAS)](https://github.com/ASPRSorg/LAS)) | Version, point data record format (PDRF), **point count**, scale and offset, **min/max XYZ (bbox)**, creation date, system ID / generating software. CRS sits in a WKT VLR/EVLR or GeoTIFF-key VLR, and EVLRs may be at the **end** of the file | laspy ([docs](https://laspy.readthedocs.io/en/latest/)) / PDAL `readers.las` ([docs](https://pdal.io/en/stable/stages/readers.las.html)) | **Yes.** Header + VLRs, plus a tail read for EVLRs |
| **LAZ** | Same `LASF` header. The compressed-PDRF bit is set and there is a LASzip VLR ([LASzip](https://laszip.org/)) | Same as LAS | laspy + `lazrs` backend / PDAL | **Yes** (header is uncompressed) |
| **COPC** | A LAZ 1.4 file with PDRF 6/7/8. The `copc` info VLR comes first at offset 375 (160 bytes). Check `copc` at byte 377 ([COPC 1.0](https://copc.io/)) | All LAS fields, plus octree center and half-size, point spacing, GPS time range, and the hierarchy of point counts per node | PDAL `readers.copc` ([docs](https://pdal.io/en/stable/stages/readers.copc.html)) / laspy (COPC support) | **Yes.** The first 589 bytes give everything; the hierarchy page adds node point counts |
| **E57** | ASCII `ASTM-E57` file signature. Binary pages with an XML section at an offset given in the header ([ASTM E2807](https://www.astm.org/e2807-11r19e01.html), [libE57Format](https://github.com/asmaloney/libE57Format)) | Scans, poses, point counts, bounds, embedded images, acquisition times. **The CRS is usually local or engineering** | pye57 ([repo](https://github.com/davidcaron/pye57)) / PDAL `readers.e57` ([docs](https://pdal.io/en/stable/stages/readers.e57.html)) | **Yes in principle** (header + XML section). **[uncertain]** whether pye57 supports remote range reads. Assume a local download |

### 1.4 Multidimensional

| Format | Detect | Metadata | Lib | Header-only? |
|---|---|---|---|---|
| **NetCDF classic / 64-bit / CDF5** | `CDF\x01` / `CDF\x02` / `CDF\x05` ([NetCDF file format spec](https://docs.unidata.ucar.edu/netcdf-c/current/file_format_specifications.html)) | Dimensions, variables, attributes. CF conventions give CRS (`grid_mapping`), time (`units: "days since..."`), bounds and standard names ([CF Conventions](https://cfconventions.org/)) | xarray + netCDF4/h5netcdf ([xarray IO](https://docs.xarray.dev/en/stable/user-guide/io.html)) | **Yes.** The header is at the front |
| **NetCDF-4 / HDF5** | HDF5 superblock signature `\x89HDF\r\n\x1a\n` at offset 0, 512, 1024, … ([HDF5 file format spec](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t3.html)) | Same as NetCDF for NetCDF-4. For generic HDF5, a group/dataset tree plus attributes | xarray / h5py ([h5py](https://docs.h5py.org/en/stable/)) | **Partly.** Metadata is scattered and needs many small reads. Works through fsspec, or index it with [kerchunk](https://fsspec.github.io/kerchunk/) / [VirtualiZarr](https://virtualizarr.readthedocs.io/) |
| **Zarr v2/v3** | Directory or prefix with `zarr.json` (v3) or `.zarray`/`.zgroup`/`.zattrs` and consolidated `.zmetadata` (v2) ([Zarr v3 spec](https://zarr-specs.readthedocs.io/en/latest/v3/core/index.html)) | Shape, chunks, dtype, codecs, attributes (often CF) | xarray + zarr-python ([docs](https://zarr.readthedocs.io/)) | **Yes.** Metadata is small JSON files |
| **GRIB 1/2** | `GRIB` at the start of each message. Byte 8 is the edition ([WMO Manual on Codes, Vol I.2](https://library.wmo.int/idurl/4/35625)) | Per message: parameter, level, reference and forecast time, grid definition (CRS + bbox) | cfgrib / ecCodes ([cfgrib](https://github.com/ecmwf/cfgrib), [ecCodes](https://confluence.ecmwf.int/display/ECC)); GDAL GRIB driver ([docs](https://gdal.org/en/stable/drivers/raster/grib.html)) | **Partly.** Messages are self-contained, but the reader has to hop from message to message using each message's length. cfgrib builds an index file **[uncertain: remote efficiency]** |

### 1.5 3D / mesh / BIM / city models

| Format | Detect | Metadata | Lib | Header-only? |
|---|---|---|---|---|
| **glTF 2.0 (.gltf)** | JSON with `asset.version: "2.0"`. External `.bin` and texture files make it a **multi-file bundle** ([glTF 2.0 spec](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html)) | `asset.generator` / `copyright`, `extensionsUsed` / `extensionsRequired` (e.g. Draco, meshopt, KTX2), scene, node, mesh, material and texture counts, and **bbox from POSITION accessor `min`/`max`**, which the spec requires for POSITION. Vertex and triangle counts come from accessor counts. **No CRS in core glTF.** Georeferencing comes from an external transform or tileset, or from extensions like `CESIUM_RTC` **[uncertain: which extensions the shop uses]** | gltf-transform (TypeScript; `inspect` CLI) ([CLI](https://gltf-transform.dev/cli), [NodeIO](https://gltf-transform.dev/modules/core/classes/NodeIO)); pygltflib (Python) ([repo](https://gitlab.com/dodgyville/pygltflib)) | **Yes.** All of it is JSON |
| **GLB** | 12-byte header: magic `0x46546C67` ("glTF"), version 2, total length. Chunk 0 is JSON (`0x4E4F534A`); chunk 1 is BIN (`0x004E4942`) ([spec §4.4 GLB](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#binary-gltf-layout)) | Same as glTF | Parse the JSON chunk by hand (about 30 lines), or use gltf-transform | **Yes, which matters for multi-GB GLBs.** Range-read bytes 0–19, read `chunkLength`, then range-read the JSON chunk. There's no need to touch the BIN chunk. The 4-byte length fields cap GLB at 4 GB, so "multi-GB" files sit close to that limit |
| **OBJ (+ .mtl)** | Text with `v`/`vt`/`vn`/`f` lines. `mtllib` names the sidecar files ([Wavefront OBJ spec (Paul Bourke mirror)](https://paulbourke.net/dataformats/obj/) **[uncertain: no official primary source exists]**) | Vertex and face counts, groups, materials. **No CRS or units** | trimesh ([docs](https://trimesh.org/)) | **No.** Full scan for counts and bbox |
| **FBX** | Binary starts `Kaydara FBX Binary  \x00` plus a version uint32. ASCII starts with `; FBX` comment lines **[uncertain: Autodesk publishes no public spec]** ([Autodesk FBX SDK](https://aps.autodesk.com/developer/overview/fbx-sdk)) | Version, creator, unit scale, axis system, mesh and material counts | Assimp ([docs](https://the-asset-importer-lib-documentation.readthedocs.io/)); Autodesk FBX SDK (proprietary) | **No.** Convert to glTF on ingest (e.g. FBX2glTF **[uncertain: maintenance status]**) |
| **USD (.usda / .usdc / .usdz)** | `.usda` starts `#usda 1.0`. `.usdc` starts `PXR-USDC`. `.usdz` is an **uncompressed zip with 64-byte-aligned files**, whose first file is the root layer ([OpenUSD usdz spec](https://openusd.org/release/spec_usdz.html)) | `upAxis`, `metersPerUnit`, default prim, prim and mesh counts, layer metadata ([OpenUSD](https://openusd.org/release/index.html)) | `usd-core` (pxr Python) ([PyPI](https://pypi.org/project/usd-core/)) | `.usda` / `.usdc` metadata needs a parse. `.usdz` root layer can be range-read because the zip is uncompressed |
| **IFC** | STEP Part 21 text starting `ISO-10303-21;`. `FILE_SCHEMA(('IFC4'))` or IFC2X3 / IFC4X3 is in the HEADER section ([ISO 10303-21 / buildingSMART IFC](https://technical.buildingsmart.org/standards/ifc/)); `.ifczip` / `.ifcxml` variants | Schema version, authoring app, project, site, building, storeys, element counts by type. Georeferencing via `IfcMapConversion` / `IfcProjectedCRS` (IFC4+) ([IFC4.3 IfcMapConversion](https://ifc43-docs.standards.buildingsmart.org/IFC/RELEASE/IFC4x3/HTML/lexical/IfcMapConversion.htm)) | IfcOpenShell ([docs](https://docs.ifcopenshell.org/)) | **Header: yes**, it's the first few hundred bytes. Georeferencing and element counts need a full parse |
| **CityGML / CityJSON** | XML in namespace `http://www.opengis.net/citygml/2.0` or `/3.0` ([OGC CityGML 3.0](https://docs.ogc.org/is/20-010/20-010.html)). CityJSON has `"type":"CityJSON"` ([CityJSON spec](https://www.cityjson.org/specs/)) | CRS (`srsName`, or CityJSON `metadata.referenceSystem`), bbox (`gml:Envelope` / `geographicalExtent`), LoD, object types | GDAL GML driver (partial) ([docs](https://gdal.org/en/stable/drivers/vector/gml.html)); cjio for CityJSON ([repo](https://github.com/cityjson/cjio)) | `gml:boundedBy` is often at the top, which helps in practice. Otherwise a full parse |
| **3D Tiles 1.0 / 1.1** | `tileset.json` with `asset.version`. Tile content is `b3dm`/`i3dm`/`pnts`/`cmpt` (4-byte ASCII magic) in 1.0, or glTF/GLB in 1.1 ([OGC 3D Tiles 1.1](https://docs.ogc.org/cs/22-025r4/22-025r4.html), [CesiumGS 3d-tiles spec](https://github.com/CesiumGS/3d-tiles)) | Root `boundingVolume` (`region` in radians on WGS84, `box`, or `sphere`), `geometricError`, tile count, extensions, metadata schema (1.1) | Read the JSON directly; [3d-tiles-tools](https://github.com/CesiumGS/3d-tiles-tools) (TypeScript) | **Yes.** `tileset.json` is small. It's a **multi-file bundle**, so ingest it as a folder or zip, or as `.3tz` **[uncertain: 3tz adoption]** |
| **I3S / SLPK** | `.slpk` is a zip (stored, no compression) with `3dSceneLayer.json.gz` and `metadata.json` ([OGC I3S 1.3](https://docs.ogc.org/cs/17-014r9/17-014r9.html), [Esri i3s-spec](https://github.com/Esri/i3s-spec)) | Layer type (3DObject, IntegratedMesh, Point, PointCloud, Building), CRS, full extent, node counts | Read the JSON directly. [i3s-converter (loaders.gl)](https://loaders.gl/docs/modules/tile-converter/cli-reference/tile-converter) converts I3S to and from 3D Tiles | **Yes.** Zip central directory (tail) plus the scene layer JSON |

### 1.6 Tabular, sensor and imagery EXIF

| Format | Detect | Metadata | Lib | Header-only? |
|---|---|---|---|---|
| **CSV / TSV** | Text. Sniff the delimiter and header row ([RFC 4180](https://datatracker.ietf.org/doc/html/rfc4180); [Python csv.Sniffer](https://docs.python.org/3/library/csv.html#csv.Sniffer)) | Columns, inferred types, row count, time range and lat/lon if present | pyarrow.csv / DuckDB `read_csv` auto-detect ([DuckDB CSV auto-detection](https://duckdb.org/docs/stable/data/csv/auto_detection)) | Schema from the head. Row count and time range need a full scan, which is cheap for small files |
| **Parquet (non-geo)** | `PAR1` (see above) | Schema, row count, row-group column min/max **statistics, which give time range for free** ([Parquet metadata](https://parquet.apache.org/docs/file-format/metadata/)) | pyarrow | **Yes** (footer) |
| **JSON Lines / NDJSON** | Text, one JSON value per line ([JSON Lines](https://jsonlines.org/)) | Inferred schema from a sample, line count | DuckDB `read_json` / pyarrow.json | Schema from the head; counts need a full scan |
| **OGC SensorThings** | An **API, not a file format**: entities Thing, Location, Datastream, Sensor, ObservedProperty, Observation, FeatureOfInterest ([OGC SensorThings API 1.1](https://docs.ogc.org/is/18-088/18-088.html)). Exported JSON follows its entity JSON | Thing/Sensor IDs, location, `phenomenonTime`, `unitOfMeasurement`, observed property | Plain JSON parsing | n/a. Readings are tiny |
| **JPEG/TIFF/HEIC photos and drone imagery with EXIF/XMP** | JPEG `FF D8 FF` with APP1 `Exif\0\0` / XMP `http://ns.adobe.com/xap/1.0/` ([EXIF (CIPA DC-008, standards index)](https://www.cipa.jp/e/std/std-sec.html), [XMP spec (Adobe)](https://developer.adobe.com/xmp/docs/)) | GPS lat/lon/alt, timestamp, camera make and model, focal length, orientation. Drone XMP adds gimbal yaw, pitch and roll **[uncertain: vendor namespaces vary, e.g. DJI]** | ExifTool (Perl CLI; widest tag coverage) ([ExifTool](https://exiftool.org/)); Pillow `getexif` ([Pillow](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.getexif)); GDAL exposes EXIF as metadata ([JPEG driver](https://gdal.org/en/stable/drivers/raster/jpeg.html)) | **Yes.** APP segments sit at the front |

### 1.7 Bio / lab

| Format | Detect | Metadata | Lib | Header-only? |
|---|---|---|---|---|
| **FASTA** | Text starting with `>` ([NCBI FASTA format](https://www.ncbi.nlm.nih.gov/genbank/fastaformat/)); often `.gz` | Sequence count, lengths, IDs and descriptions. **No spatial or time fields.** Sample location or date comes from a sidecar or LIMS | Biopython `SeqIO` ([docs](https://biopython.org/wiki/SeqIO)) | No. Full scan, which is fine for typical sizes |
| **FASTQ** | Records of 4 lines starting with `@`, with a `+` separator ([Cock et al. 2010, NAR](https://academic.oup.com/nar/article/38/6/1767/3112533)) | Read count, read lengths, quality encoding, instrument and run ID parsed from Illumina headers **[uncertain: header conventions vary by instrument]** | Biopython / pysam ([pysam](https://pysam.readthedocs.io/)) | No |
| **Instrument CSV / XLSX exports** | Sniff as CSV. XLSX is a zip containing `[Content_Types].xml` and `xl/` ([ECMA-376](https://ecma-international.org/publications-and-standards/standards/ecma-376/)) | Columns, units (often in the header rows), sample IDs, timestamps | pandas / openpyxl | Head only for the schema |

### 1.8 Generic content detectors (the first pass)

| Tool | Notes | Fit |
|---|---|---|
| **libmagic** (`file`) via `python-magic` | Signature database. Knows TIFF, HDF5, NetCDF, SQLite, zip, glTF binary, etc. ([file(1) / magic(5)](https://www.darwinsys.com/file/), [python-magic](https://github.com/ahupp/python-magic)) | Fast, local, C. **Does not distinguish COG from GeoTIFF, GPKG from SQLite, COPC from LAZ, or KMZ/SLPK/USDZ from zip.** Layer our own rules on top |
| **Apache Tika** | Java server. Detection plus text and metadata parsers. Has a GDAL parser wrapper and ISO 19139 support ([Tika supported formats](https://tika.apache.org/3.0.0/formats.html)) | Heavy (JVM) and adds little over GDAL for geo. Useful for office docs and PDFs if the shop ingests those |
| **Google Magika** | Deep-learning file-type classifier ([repo](https://github.com/google/magika)) | Good for text-ish files (CSV vs JSON vs code). **[uncertain]** coverage of geo binaries. Optional tiebreaker |
| **GDAL Identify** | `gdal.IdentifyDriverEx(path)` asks every driver; works through `/vsiaz/` ([GDAL API](https://gdal.org/en/stable/api/python/raster_api.html)) | The authoritative "is this geo, and which driver" check |

---

## 2. Remote / range-read mechanics (Azure Government)

- **GDAL `/vsiaz/`** reads blobs with HTTP range requests. It uses 16 KB chunks by default, growing on sequential reads, with a shared LRU cache (`CPL_VSIL_CURL_CHUNK_SIZE`, `CPL_VSIL_CURL_CACHE_SIZE`) ([GDAL VSI docs](https://gdal.org/en/stable/user/virtual_file_systems.html)).
- **Chaining:** `/vsizip//vsiaz/container/bundle.zip/roads.shp` opens a zipped shapefile in place, because the zip central directory is read by a range read ([GDAL VSI docs](https://gdal.org/en/stable/user/virtual_file_systems.html)).
- **Auth options, in order:** connection string; then account + access token / key / SAS / no-sign; then managed identity via IMDS; then workload identity (`AZURE_FEDERATED_TOKEN_FILE`, `AZURE_AUTHORITY_HOST`); then the `az` CLI config ([GDAL VSI docs](https://gdal.org/en/stable/user/virtual_file_systems.html)).
- **Azure Government gotchas, confirmed in GDAL source** ([port/cpl_azure.cpp](https://github.com/OSGeo/gdal/blob/master/port/cpl_azure.cpp)):
  - Without an override, the endpoint is built as `https://<account>.blob.core.windows.net`. **Set `CPL_AZURE_ENDPOINT=https://<account>.blob.core.usgovcloudapi.net`**, or use a connection string with `EndpointSuffix=core.usgovcloudapi.net` or `BlobEndpoint=` (both are parsed by GDAL).
  - The IMDS and workload-identity token requests hardcode `resource` / `scope = https://storage.azure.com/`. **[uncertain]** whether that audience is accepted in Azure Government; check Microsoft's [Azure Government developer guide](https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure). The safest pattern is to get the token ourselves with `azure-identity` (authority `login.microsoftonline.us`) and pass it as `AZURE_STORAGE_ACCESS_TOKEN`, or generate a user-delegation SAS ([azure-identity](https://learn.microsoft.com/en-us/python/api/overview/azure/identity-readme)).
- **Non-GDAL readers** (pyarrow, laspy, h5py, zarr, a custom GLB reader) go through **fsspec + adlfs** (`abfs://`), which supports range reads and an `account_host` override for sovereign clouds ([adlfs](https://github.com/fsspec/adlfs), [fsspec](https://filesystem-spec.readthedocs.io/)). **[uncertain: exact adlfs kwarg for the Gov endpoint; verify]**

**Header-read budget by format.** These are approximate, so measure in a spike:

| Format | Bytes needed for metadata |
|---|---|
| COG | ~16–64 KB (first IFDs) |
| LAS/LAZ/COPC | 589 B to a few KB, plus a tail read for EVLRs |
| GLB | 20 B, then `chunkLength` B (the JSON chunk: KB to a few MB) |
| Parquet / GeoParquet | 8 B tail, then the footer (KB–MB) |
| FlatGeobuf | header (KB) |
| Zip bundles (KMZ / SLPK / USDZ / zipped SHP or GDB) | End of central directory (≤64 KB tail), then the central directory, then member headers |
| Zarr | `zarr.json` / `.zmetadata` |
| GeoJSON / KML / OBJ / FBX / CSV / FASTx | Whole file (stream it) |

---

## 3. Cloud-native targets (convert on ingest)

| Target | From | Tool | Notes |
|---|---|---|---|
| **COG** | GeoTIFF, JP2, NITF, MrSID, ECW, IMG, … | `gdal_translate -of COG` ([GDAL COG driver](https://gdal.org/en/stable/drivers/raster/cog.html)); `rio cogeo create` ([rio-cogeo](https://cogeotiff.github.io/rio-cogeo/)) | OGC-standardized ([OGC 21-026](https://docs.ogc.org/is/21-026/21-026.html)). Choose compression per type: DEFLATE/ZSTD/LERC for elevation, JPEG/WEBP for RGB |
| **COPC** | LAS, LAZ, E57 | PDAL `writers.copc` ([docs](https://pdal.io/en/stable/stages/writers.copc.html)); Untwine (handles large inputs out of core) ([repo](https://github.com/hobuinc/untwine)) | STAC media type `application/vnd.laszip+copc` ([STAC best practices](https://github.com/radiantearth/stac-spec/blob/master/best-practices.md)) |
| **GeoParquet** | Shapefile, GPKG, GeoJSON, KML, FGDB, CSV | `ogr2ogr -f Parquet` ([GDAL Parquet](https://gdal.org/en/stable/drivers/vector/parquet.html)); geopandas `to_parquet` ([docs](https://geopandas.org/en/stable/docs/reference/api/geopandas.GeoDataFrame.to_parquet.html)) | Also good for IoT and lab tabular data (plain Parquet). Pick 1.1 or 2.0 deliberately (see §1.2) |
| **FlatGeobuf / PMTiles** (optional) | Vector | GDAL FGB driver; tippecanoe → PMTiles ([tippecanoe](https://github.com/felt/tippecanoe), [PMTiles spec](https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)) | For web map display, not for analysis |
| **Zarr** | NetCDF, HDF5, GRIB | `xarray.Dataset.to_zarr` ([docs](https://docs.xarray.dev/en/stable/generated/xarray.Dataset.to_zarr.html)); or *virtual* Zarr references with no data copy via [VirtualiZarr](https://virtualizarr.readthedocs.io/) / [kerchunk](https://fsspec.github.io/kerchunk/) | Virtual references are worth considering for multi-GB archives (index without duplicating) |
| **3D Tiles** | GLB, glTF, OBJ, FBX, IFC, CityGML, point clouds, I3S | [3d-tiles-tools](https://github.com/CesiumGS/3d-tiles-tools) (packaging and upgrade); [py3dtiles](https://py3dtiles.org/) (point clouds and IFC → 3D Tiles **[uncertain: IFC maturity]**); [pg2b3dm](https://github.com/Geodan/pg2b3dm) (PostGIS → tiles); loaders.gl tile-converter (I3S ↔ 3D Tiles). For FBX/OBJ, first go to GLB with [obj2gltf](https://github.com/CesiumGS/obj2gltf) / Assimp | **Gap:** there's no great open-source tool for tiling one huge GLB mesh into an LOD hierarchy. Cesium ion does it but is commercial SaaS **[uncertain: availability for Azure Government / on-prem]**. A single GLB under 4 GB can be wrapped in a one-tile tileset as a stopgap |

**Policy:** keep the original bytes immutable as the canonical asset, with a hash. Register the cloud-native copy as an alternate asset with `processing:*` lineage. Conversion runs asynchronously after metadata extraction, so files become searchable before conversion finishes.

---

## 4. STAC as the metadata model

**Version:** STAC 1.1.0, released 2024-09-10 ([CHANGELOG](https://github.com/radiantearth/stac-spec/blob/master/CHANGELOG.md)).

### 4.1 Core fields

- **Item** ([item-spec](https://github.com/radiantearth/stac-spec/blob/master/item-spec/item-spec.md)): `type: "Feature"`, `stac_version`, `id`, `geometry` (**may be `null`**), `bbox` (required if geometry is non-null, **prohibited if it's null**), `properties.datetime` (may be `null` only if `start_datetime` and `end_datetime` are set), `links`, `assets`, `collection`.
- **Common metadata** ([common-metadata](https://github.com/radiantearth/stac-spec/blob/master/commons/common-metadata.md)):
  - Time: `created`, `updated`, `start_/end_datetime`
  - Rights and provenance: `license` (SPDX), `providers`
  - Instrument: `platform`, `instruments`, `constellation`, `mission`, `gsd`
  - Values: `data_type`, `nodata`, `statistics`, `unit`
  - Description: `keywords`, `roles`
  - **In 1.1, `bands` replaced `eo:bands` and `raster:bands`** ([CHANGELOG](https://github.com/radiantearth/stac-spec/blob/master/CHANGELOG.md)).
- **Collection** ([collection-spec](https://github.com/radiantearth/stac-spec/blob/master/collection-spec/collection-spec.md)): `id`, `description`, `license`, `extent.spatial` / `extent.temporal`, `summaries`, `item_assets` (core since 1.1).
- **Media types** for COG, COPC, GeoParquet, 3D Tiles and PMTiles are listed in [best practices](https://github.com/radiantearth/stac-spec/blob/master/best-practices.md).

### 4.2 Extensions

Versions and maturity are from the [stac-extensions registry](https://stac-extensions.github.io/) as of this writing.

| Extension | Ver / maturity | Use here |
|---|---|---|
| [projection](https://github.com/stac-extensions/projection) (`proj:`) | 2.1.0 Stable | `proj:code` (e.g. `EPSG:32618`), `proj:wkt2` / `proj:projjson`, `proj:bbox`, `proj:shape`, `proj:transform`. Normalize with pyproj ([pyproj CRS](https://pyproj4.github.io/pyproj/stable/api/crs/crs.html)) |
| [raster](https://github.com/stac-extensions/raster) | 2.0.0 Candidate | Per-band `raster:` fields under 1.1 `bands` (bit depth, scale and offset, spatial resolution) |
| [eo](https://github.com/stac-extensions/eo) | 2.0.0 Stable | `eo:cloud_cover`, band `eo:common_name` / `center_wavelength` for imagery |
| [pointcloud](https://github.com/stac-extensions/pointcloud) (`pc:`) | 2.0.0 Pilot | `pc:count`, `pc:type` (lidar, sonar, …), `pc:encoding`, `pc:schemas`, `pc:statistics`. PDAL has no STAC writer ([writers list](https://pdal.io/en/stable/stages/writers.html)), so map `pdal info --metadata` output ourselves |
| [file](https://github.com/stac-extensions/file) | 2.1.0 Stable | `file:size`, `file:checksum` (multihash), `file:header_size`. **Use on every asset** |
| [processing](https://github.com/stac-extensions/processing) | 1.3.0 Candidate | `processing:software`, `processing:lineage`, `processing:datetime`. Records the conversion pipeline |
| [version](https://github.com/stac-extensions/version) | 1.2.0 Candidate | `version`, `deprecated`, `latest-version` / `predecessor-version` links. Handles re-uploads of the same logical dataset |
| [table](https://github.com/stac-extensions/table) | 1.3.0 Pilot | `table:columns`, `table:row_count`, `table:primary_geometry`. Fits GeoParquet, CSV and lab tables |
| [datacube](https://github.com/stac-extensions/datacube) (`cube:`) | 2.3.0 Candidate | Dimensions and variables for NetCDF, Zarr and GRIB. Produce with [xstac](https://github.com/stac-utils/xstac) |
| [alternate-assets](https://github.com/stac-extensions/alternate-assets) | 1.2.0 Pilot | Original vs. cloud-native copy, or blob URL vs. HTTPS URL |
| [storage](https://github.com/stac-extensions/storage) | 2.0.0 Pilot | Storage platform, region and tier per asset (Azure Gov blob tier) |
| [timestamps](https://github.com/stac-extensions/timestamps) | 1.1.0 Pilot | `published`, `expires`, `unpublished` |
| Mesh / 3D | **None stable.** 3D City Models is at 0.2.0 Proposal ([registry](https://stac-extensions.github.io/)) | Define our own `mesh:` fields: vertex count, triangle count, `extensionsUsed`, generator, up-axis, units |

### 4.3 Non-spatial data (bio, IoT)

- **Structurally STAC fits.** Use `geometry: null` with no `bbox`, plus `datetime` or a start/end range ([item-spec](https://github.com/radiantearth/stac-spec/blob/master/item-spec/item-spec.md)). Domain fields go in a namespaced project extension (`itw:sample_id`, `itw:instrument`, `itw:assay`) published as a JSON Schema so `stac-validator` can enforce it.
- **Don't make one Item per IoT reading.** Kilobyte readings should land in a time-series or table store. The searchable record is then a **batch or partition Item** (e.g. one per device per day) whose asset is a Parquet file. Use `table:` columns, a time range and the device location as geometry. For live device data the OGC model is SensorThings ([OGC 18-088](https://docs.ogc.org/is/18-088/18-088.html)); STAC indexes its exports, not each Observation.
- **Alternative: OGC API – Records**, a general catalog-record model that also permits null geometry ([OGC API – Records](https://ogcapi.ogc.org/records/)). **[uncertain: Part 1 approval status as of 2026]** STAC is the better choice because its tooling is far more mature: [pystac](https://pystac.readthedocs.io/), [pgstac](https://github.com/stac-utils/pgstac), [stac-fastapi](https://github.com/stac-utils/stac-fastapi). Records can be exposed later as a view if needed.

---

## 5. Recommended pipeline

```
upload (blob, original immutable, sha256)
  │
  ├─1 SNIFF   range-read head 4 KB + tail 64 KB
  │           → magic table (ours) → libmagic fallback → extension tiebreak
  │           → zip? list central directory → re-sniff members (KMZ/SLPK/USDZ/SHP/GDB/3D Tiles/xlsx)
  │           → bundle grouping: same-basename siblings (.shp/.dbf/.shx/.prj; .gltf+.bin; .obj+.mtl)
  │
  ├─2 CLASSIFY  → {family: raster|vector|pointcloud|multidim|mesh|tabular|image|bio|unknown,
  │                format, driver, is_cloud_native, needs_proprietary_sdk}
  │           → routing decision (blob tier / table / time-series)
  │
  ├─3 EXTRACT   per-family extractor plugin, header-only first (§2), full-scan only if small
  │           raster: rasterio/GDAL   vector: pyogrio/GDAL   pc: PDAL/laspy
  │           multidim: xarray(+cfgrib/h5netcdf/zarr)   mesh: GLB-json reader / gltf-transform / ifcopenshell / usd-core
  │           tabular: pyarrow/DuckDB   image: ExifTool   bio: Biopython
  │
  ├─4 NORMALIZE  → STAC 1.1 Item: geometry = footprint reprojected to EPSG:4326 (pyproj),
  │           proj:* keeps native CRS, file:* on every asset, family-specific ext (pc/raster/cube/table/mesh)
  │           → validate (stac-validator / JSON Schema) → upsert to catalog (pgstac) + search index
  │
  └─5 CONVERT (async)  → COG / COPC / GeoParquet / Zarr(virtual) / 3D Tiles
              → add alternate asset + processing:* lineage → bump updated
```

### Design notes

- **Extractor plugin interface.** Use something like `can_handle(sniff_result) -> confidence` plus `extract(uri, budget) -> PartialItem`. Each extractor declares whether it can work header-only. The orchestrator passes a byte or time budget. Over budget, it records `extraction_level: "header"` and queues a full scan.
- **Footprint.** Use the bbox reprojected to WGS84, densifying the edges with `pyproj.Transformer.transform_bounds` ([docs](https://pyproj4.github.io/pyproj/stable/api/transformer.html#pyproj.transformer.Transformer.transform_bounds)). Better footprints are optional, e.g. `rio-stac` or `pdal --boundary`, which costs a full read ([pdal info](https://pdal.io/en/latest/apps/info.html)).
- **Local and engineering CRS.** Meshes (OBJ/FBX/GLB), E57 and often IFC have no geographic CRS. Store `geometry: null` plus a `mesh:local_bbox`. Let users attach georeferencing later, as a new version.
- **Language split.**
  - Python workers own steps 1–5. GDAL, PDAL, rasterio and pyogrio ship as wheels or conda packages; [rasterio wheels bundle GDAL](https://rasterio.readthedocs.io/en/stable/installation.html).
  - Use a single **container image pinned to one GDAL build**, e.g. the official [GDAL Docker images](https://github.com/OSGeo/gdal/tree/master/docker).
  - The TypeScript side covers gltf-transform or 3d-tiles-tools as CLI subprocesses, and the UI (e.g. `flatgeobuf` and `@loaders.gl` for preview).
- **Proprietary formats.** MrSID and ECW (SDKs), plus FBX for full fidelity: isolate them in a separate optional worker image for licensing reasons. Without it, accept and store the file with a minimal record (`itw:extraction_status: "unsupported_driver"`).
- **Security.** Uploaded files are untrusted input to C/C++ parsers (GDAL, PDAL, Assimp). Run extractors in sandboxed, network-restricted, resource-limited containers. Watch for zip bombs: cap the uncompressed size and member count before extracting. NITF classification fields should map to access labels.
- **Testing.** Build a corpus with one small real file per format, plus edge cases: TIFF without CRS, shapefile missing `.prj`, LAS 1.2 vs 1.4, GLB with Draco, GeoParquet 1.1 vs 2.0, a Zarr v2/v3 mix. Use golden STAC JSON per file. GDAL's autotest data is a good source of small samples ([gdal/autotest](https://github.com/OSGeo/gdal/tree/master/autotest)).

### Library shortlist

| Need | Python | TypeScript |
|---|---|---|
| Raster | rasterio, GDAL `osgeo`, rio-cogeo, rio-stac ([rio-stac](https://github.com/developmentseed/rio-stac)) | geotiff.js ([repo](https://github.com/geotiffjs/geotiff.js)) for preview only |
| Vector | pyogrio, geopandas, pyarrow, DuckDB spatial | flatgeobuf, @loaders.gl |
| Point cloud | PDAL python ([docs](https://pdal.io/en/stable/python.html)), laspy + lazrs | copc.js ([repo](https://github.com/connormanning/copc.js)) |
| Multidim | xarray, zarr, h5netcdf, cfgrib, VirtualiZarr, xstac | zarrita.js **[uncertain]** |
| Mesh / BIM | pygltflib, trimesh, ifcopenshell, usd-core | gltf-transform, 3d-tiles-tools |
| CRS | pyproj | proj4js (limited) |
| Catalog | pystac, stac-validator, pgstac, stac-fastapi | stac-js **[uncertain]** |
| Detection | python-magic, custom magic table, optional Magika | — |
