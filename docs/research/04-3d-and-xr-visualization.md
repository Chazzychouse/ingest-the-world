# 04 — Large 3D Models, Tiling, and AR/VR/Web Visualization

Researched 2026-10-24. Primary sources are cited inline. **[DATE]** marks status that changes over time; re-check it before you rely on it. **[UNVERIFIED]** marks claims I inferred or could not confirm from a primary source.

---

## 1. glTF 2.0 / GLB structure and cheap metadata reads

### Binary layout (glTF 2.0 spec, §4 "GLB File Format Specification")
Source: https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html (text at https://github.com/KhronosGroup/glTF/blob/main/specification/2.0/Specification.adoc)

| Offset | Size | Field | Value / rule |
|---|---|---|---|
| 0 | uint32 LE | `magic` | MUST be `0x46546C67` (ASCII `glTF`) |
| 4 | uint32 | `version` | `2` (container version, not the asset version) |
| 8 | uint32 | `length` | total file length, header + all chunks |
| 12 | uint32 | chunk0 `chunkLength` | length of the JSON chunk data |
| 16 | uint32 | chunk0 `chunkType` | `0x4E4F534A` (`JSON`), MUST be first, exactly 1 |
| 20 | … | JSON bytes | padded with trailing spaces `0x20` to 4-byte alignment |
| … | uint32 ×2 | chunk1 length/type | `0x004E4942` (`BIN\0`), MUST be second, 0 or 1 occurrence |
| … | … | BIN bytes | padded with `0x00`; clients MUST ignore unknown chunk types |

- Size limit: the spec's Buffers section says a GLB-stored buffer is implicitly limited because "the entire file may be at most 2^32-4 bytes" (about 4 GiB). This follows from the uint32 `length`. **A conforming GLB therefore cannot exceed about 4 GiB.** Larger "GLB" files are non-conformant, or need `.gltf` + external `.bin` buffers (separate `uri`s, each a separate buffer). Plan for 2–4 GB inputs near the ceiling, plus possible malformed >4 GB files.
- Units are meters. The frame is right-handed, +Y up, +Z forward (spec §3.4). **glTF has no CRS field.** Georeferencing is always external to core glTF.

### Range-read recipe for metadata (no full download)
1. `GET Range: bytes=0-19` → validate magic/version, read `length` (whole-file size check) and `chunkLength` of the JSON chunk.
2. `GET Range: bytes=20-(20+chunkLength-1)` → parse JSON. You get `asset.generator/version/copyright`, `extensionsUsed/Required`, mesh/node/material/texture counts, accessor `min`/`max` (POSITION accessors MUST define min/max, so a bounding box is available without decoding geometry), `extras`, and any geo extensions.
3. Skip the BIN chunk entirely. Azure Blob supports HTTP Range on GET (standard). JSON chunks are usually KB–low MB even for multi-GB files. **[UNVERIFIED]** This is typical, not guaranteed: huge scenes with millions of nodes can have tens of MB of JSON.
4. Derive these flags for the catalog: `requiresDraco`, `requiresMeshopt`, `hasKTX2`, `triangleEstimate` (sum of index accessor counts/3), `textureCount`, bounds, and whether images are embedded (bufferView) or external (uri).

### Extensions that matter (Khronos registry: https://github.com/KhronosGroup/glTF/blob/main/extensions/README.md)

| Extension | Status | Purpose | Notes |
|---|---|---|---|
| `KHR_draco_mesh_compression` | Ratified | Geometry compression (Google Draco) | Best ratio, slower decode, needs WASM decoder |
| `EXT_meshopt_compression` | Ratified (multi-vendor) | Geometry + animation compression | Fast decode, gzip-friendly; gltfpack also emits `KHR_meshopt_compression` with `-cz` **[DATE]** (https://github.com/zeux/meshoptimizer/blob/master/gltf/README.md) |
| `KHR_mesh_quantization` | Ratified | Integer/normalized vertex attributes | gltfpack default unless `-noq` |
| `KHR_texture_basisu` | Ratified | KTX2 + Basis Universal textures (GPU-transcodable) | The big win for VRAM on headsets/mobile |
| `EXT_mesh_gpu_instancing` | Ratified | Instanced nodes | gltfpack `-mi`, gltf-transform `instance` |
| `EXT_structural_metadata`, `EXT_mesh_features` | Cesium-authored, used by 3D Tiles 1.1 (not in Khronos ratified list) | Per-feature/per-vertex metadata, property tables | https://github.com/CesiumGS/glTF/tree/main/extensions/2.0/Vendor |
| `CESIUM_RTC` | Vendor, written against **glTF 1.0** | Relative-to-center positions for precision | Legacy. https://github.com/CesiumGS/glTF/tree/3d-tiles-next/extensions/1.0/Vendor/CESIUM_RTC |

### Georeferencing options (glTF has none natively)
| Approach | How | When |
|---|---|---|
| 3D Tiles `tile.transform` | 4×4 column-major matrix from local ENU to ECEF (EPSG:4978) on the root tile. glTF y-up is rotated to z-up at runtime (https://github.com/CesiumGS/3d-tiles/blob/main/specification/README.adoc) | **Recommended.** Works with every 3D Tiles viewer |
| Sidecar metadata | Store `{lon, lat, height, heading, pitch, roll, sourceCRS}` in the catalog DB/STAC item. The viewer builds the ENU→ECEF matrix (e.g. `Cesium.Transforms.eastNorthUpToFixedFrame`) | Single-model placement, the simplest option for ingest |
| `CESIUM_RTC` / b3dm `RTC_CENTER` | Center offset baked into content | Legacy 1.0 content only. Avoid for new output |
| `asset.extras` / node `extras` | Ad-hoc, non-standard | Only as a carrier when upstream tools (e.g. drone/photogrammetry exports) put geo info there |

Ingest implication: most GLBs arrive **without** georeference. The pipeline needs (a) automatic sniffing (extras, a sidecar `.prj`/`.json`/`.xml` from photogrammetry tools, or a companion 3D Tiles tileset) and (b) a manual "place on map" UI that writes the transform.

---

## 2. Optimization pipeline for huge models

| Tool | What it gives you | Notes / limits |
|---|---|---|
| **glTF-Transform** CLI/API (`@gltf-transform/cli`, `@gltf-transform/core`, MIT) — https://gltf-transform.dev/cli | `inspect` (perf report), `optimize` (all-in-one), `draco`, `meshopt`, `quantize`, `simplify`, `weld`, `dedup`, `prune`, `instance`, `join`, `resize`, `webp`, `etc1s`/`uastc` (KTX2), `partition` (split buffers to .bin), `merge` | Node.js, so the whole document is in memory. Multi-GB inputs need large heaps (`--max-old-space-size`). **[UNVERIFIED]** The docs give no large-file guidance. KTX2 (`etc1s`/`uastc`) historically shells out to KTX-Software `ktx`/`toktx`. **[UNVERIFIED on current version]** Install KTX-Software in the container either way. Image work uses `sharp`. |
| **gltfpack** (meshoptimizer, MIT) — https://github.com/zeux/meshoptimizer/blob/master/gltf/README.md | `-c`/`-cc` meshopt compression, quantization by default, `-si R` simplify, `-tc` KTX2/BasisU, `-tw` WebP, `-mi` instancing, `-kn`/`-km` keep names/materials | Native C++. The README says native binaries "handle larger files" than the npm build and include texture compression. **Best choice for multi-GB inputs.** Drops unknown/vendor extensions, so preserve metadata separately. |
| **Draco** (via glTF-Transform `draco` or Blender exporter) | Highest geometry ratio | Slower decode. Prefer meshopt for streaming/XR, where CPU decode budget matters. |
| **KTX2/BasisU** | ETC1S (small, lower quality) or UASTC (high quality) | Main lever for headset VRAM. Transcoders are built into three.js (`KTX2Loader`), CesiumJS, Babylon. |

### LOD
- Single-file LOD: `gltf-transform simplify` / `gltfpack -si 0.5`, `0.1`, … produce discrete LOD files. Store them as derivatives (`lod0`, `lod1`, `lod2`). This is fine for models up to a few hundred MB.
- **Streaming LOD (spatial tiling) is required for multi-GB.** See §3. Simplifying a 3 GB file to one 50 MB proxy is also a useful "preview" derivative.

### Thumbnails / turntables (headless)
| Option | Notes |
|---|---|
| Headless Chromium (Playwright/Puppeteer) loading a three.js or `<model-viewer>` page, `toDataURL`/screenshot, then ffmpeg for turntable MP4 | Same renderer as the viewer, so visual parity. Needs GPU or SwiftShader/software GL. Slow on CPU but fine for a 512² thumbnail of the **simplified** proxy. **[UNVERIFIED]** I did not check perf numbers. |
| Blender `blender -b -P render.py` (glTF importer, Eevee/Cycles) | Robust, scriptable, good lighting. Heavy container (~1 GB). Imports Draco; meshopt support depends on version **[UNVERIFIED]**. |
| Always render from the decimated proxy, never the raw multi-GB GLB | Keeps workers small and fast |

Recommended ingest worker sequence: range-read header/JSON → validate (Khronos glTF-Validator, https://github.com/KhronosGroup/glTF-Validator) → `gltfpack -si` proxy + KTX2 → thumbnail from proxy → tile full-res to 3D Tiles (§3) → write catalog record.

---

## 3. Tiling for streaming

### OGC 3D Tiles 1.1
- An OGC Community Standard. 1.0 was approved 2018-12-14. 1.1 adds **glTF as direct tile content**, multiple contents per tile, structured metadata, and **implicit tiling** (quadtree/octree with availability in fixed-size subtrees). It deprecates b3dm/i3dm/pnts/cmpt (https://github.com/CesiumGS/3d-tiles). OGC doc 22-025r4 (https://docs.ogc.org/cs/22-025r4/22-025r4.html). **[UNVERIFIED adoption date, ~Jan 2023]**
- Core concepts: `boundingVolume` (region radians / box / sphere), `geometricError` (meters), `refine` ADD/REPLACE, `transform` (https://github.com/CesiumGS/3d-tiles/blob/main/specification/README.adoc).

### Converters: which actually tile an arbitrary mesh?

| Tool | Splits/LODs arbitrary mesh? | Input | Output | License | Verdict |
|---|---|---|---|---|---|
| **CesiumGS 3d-tiles-tools** — https://github.com/CesiumGS/3d-tiles-tools | **No.** `createTilesetJson` on one file gives "a single (root) tile with the input file as its tile content" | GLB, b3dm, i3dm, tilesets | Tileset utilities: `upgrade`, `convert` (3tz/dir), `merge`, `combine`, `glbToB3dm`, `analyze`, `serve`, `pipeline` | Apache-2.0 | Use for post-processing, packaging and validation, **not** tiling |
| **mago-3d-tiler** (Gaia3D) — https://github.com/Gaia3D/mago-3d-tiler | **Yes.** Max triangles per node (`-mx`), LOD range, explicit or implicit tiling | glTF/GLB, OBJ, FBX, DAE, 3DS, CityGML, IndoorGML, IFC (partial), LAS/LAZ, SHP/GeoJSON/GPKG extrusion | 3D Tiles 1.1 (default) | MPL-2.0 (commercial option) | **Best open-source fit for GLB ingest.** Java 21 / Docker image `gaia3d/mago-3d-tiler`. CRS via `-c EPSG`, `-lon/-lat`, proj4 |
| **Obj2Tiles** (OpenDroneMap) — https://github.com/OpenDroneMap/Obj2Tiles | **Yes.** Quadric decimation LODs + recursive XY(Z) split, `--octree` | **OBJ only** | B3DM default, `--glb` gives 3D Tiles 1.1 | AGPL-3.0 | Works with GLB→OBJ pre-conversion (loses PBR extensions). Placement via `--lat/--lon/--alt`. Note: **defaults to Milan** if lat/lon are omitted |
| **py3dtiles** — https://py3dtiles.org/ (v12.1.1) | Point clouds yes. Meshes: IFC/OBJ/WKB "geometries", GLB not stated | las/laz/xyz/ply/csv, wkb, ifc, obj | tileset.json + pnts/b3dm (1.1 status not stated) | Apache-2.0 **[UNVERIFIED]** | Good for **point clouds**, not the GLB path |
| **Cesium ion / ion Self-Hosted** — https://cesium.com/learn/ion/self-hosted/ | Yes (Design/Reality tilers) | Many | 3D Tiles 1.1 | **Commercial**, `CESIUM_LICENSE` env, contact sales | Gold standard and container/K8s deployable, but not free. Mention as the "enterprise upgrade path" |

### I3S (alternative)
- OGC I3S 1.3 Community Standard, approved 2022-12-12, published 2023-01-11. Packages as `.slpk` (https://docs.ogc.org/cs/17-014r9/17-014r9.html). It is Esri-centric. deck.gl `Tile3DLayer` reads I3S via `I3SLoader` (https://deck.gl/docs/api-reference/geo-layers/tile-3d-layer). **Recommendation: only add I3S export if the client is an ArcGIS shop.** 3D Tiles has broader open viewer support.

---

## 4. Viewers

| Viewer | Strengths | Weaknesses for this project | Sources |
|---|---|---|---|
| **CesiumJS** (Apache-2.0, docs at v1.146 **[DATE]**) | True WGS84 globe, terrain, 3D Tiles 1.1 reference implementation, KTX2/Draco/meshopt, imagery providers (WMS/WMTS/TMS/UrlTemplate/ArcGIS/Bing/OSM) | **No WebXR.** `Scene.useWebVR` is only side-by-side stereo "for cardboard and WebVR". The WebXR PR #11372 has been open since 2023-06, and maintainers said in Oct 2024 they "haven't been able to prioritize" it **[DATE]** | https://cesium.com/learn/cesiumjs/ref-doc/Scene.html, https://github.com/CesiumGS/cesium/pull/11372 |
| **three.js + 3DTilesRendererJS** (NASA-AMMOS, Apache-2.0, npm `3d-tiles-renderer`) | Streams 3D Tiles into a plain three.js scene, with packages for three, Babylon and R3F. Plugins: TMS/XYZ/WMTS/WMS imagery, quantized-mesh terrain, GeoJSON, vector tiles. Has a **VR example**, and three.js has first-class WebXR | Less "GIS-complete" than Cesium (no built-in measurement tools, fewer CRS helpers). Assemble your own UI | https://github.com/NASA-AMMOS/3DTilesRendererJS |
| **Babylon.js** | Strong built-in WebXR (`createDefaultXRExperienceAsync`, features manager: hit-test, anchors, plane detection) plus 3DTilesRendererJS babylon package | Smaller geo ecosystem | https://doc.babylonjs.com/features/featuresDeepDive/webXR/introToWebXR |
| **deck.gl / loaders.gl** | `Tile3DLayer` (3D Tiles + I3S), great 2D overlays for vectors/rasters on MapLibre | 3D Tiles 1.1 glTF-content support not stated on the docs page **[UNVERIFIED]**. No XR | https://deck.gl/docs/api-reference/geo-layers/tile-3d-layer |
| **Potree** | Large point-cloud web viewer (Potree octree, LAS/LAZ, EPT), VR examples | COPC support not stated in README **[UNVERIFIED]**. Maintenance cadence unclear (Potree-Next in development) | https://github.com/potree/potree, https://copc.io/ |

### Base layers in Azure Government
- The Azure Maps Web SDK supports Azure Government via the `atlas.azure.us` domain with Gov auth (https://learn.microsoft.com/en-us/azure/azure-maps/how-to-use-map-control). Raster tiles can feed CesiumJS through `UrlTemplateImageryProvider` (there is no built-in Azure Maps provider; https://cesium.com/learn/cesiumjs/ref-doc/ImageryProvider.html). Confirm that the Azure Maps **Render/tile API** (not just the SDK) is enabled in your Gov region. **[DATE/UNVERIFIED]** Azure Maps web control v1 was retiring 2026-09-19, so use v3.
- Gov/offline-friendly: self-host imagery as COG → XYZ/WMTS (TiTiler/GeoServer), USGS National Map services, NAIP. Terrain: quantized-mesh from your own DEM (e.g. via open tools like `cesium-terrain-builder` **[UNVERIFIED maintenance]**) or Cesium ion (commercial). Avoid default Cesium ion / Bing tokens in a Gov deployment.

---

## 5. AR / VR

### WebXR spec status
- WebXR Device API: W3C **Candidate Recommendation Snapshot, 31 March 2022**. It defines `inline`, `immersive-vr`, `immersive-ar`, and `immersive-ar` behavior is in the separate AR Module (https://immersive-web.github.io/webxr/, https://www.w3.org/TR/2022/CR-webxr-20220331/). **[DATE]**
- Anchors Module: Editors' Draft (27 Apr 2026). It has **persistent anchors** (`requestPersistentHandle`, `restorePersistentAnchor`), but **no geospatial (lat/lon) anchors** (https://immersive-web.github.io/anchors/). **There is no standard WebXR geospatial anchoring.** Web AR "on the real world" = local placement (hit-test on floor/table) + manual alignment, or GPS/compass approximations.

### Device/browser matrix **[DATE — all rows]**

| Platform | immersive-vr | immersive-ar | hit-test / anchors / planes | Notes / sources |
|---|---|---|---|---|
| **Meta Quest Browser** (Quest 2/3/Pro) | Yes | Yes (passthrough; transparent background required) | Plane detection, persistent anchors (max 8 per site), hit-test, mesh detection, depth sensing (release notes v39+) | https://developers.meta.com/horizon/documentation/web/webxr-mixed-reality/, https://developers.meta.com/horizon/release-notes/web/ — **Best web XR target** |
| **Apple Vision Pro Safari** | Yes since visionOS 2 / Safari 18 (transient-pointer, hands) | **No.** Still "limited to immersive-vr only" at WWDC26 for visionOS 27 | n/a | https://webkit.org/blog/15865/webkit-features-in-safari-18-0/, https://developer.apple.com/videos/play/wwdc2026/8004/. Alternative: the HTML `<model>` element (USDZ, stereoscopic in page) from visionOS 26, coming to iOS/macOS in Safari 27 (https://webkit.org/blog/17333/webkit-features-in-safari-26-0/, https://developer.apple.com/videos/play/wwdc2026/215/) |
| **Android Chrome** (ARCore devices) | phone n/a | Yes (needs Google Play Services for AR, HTTPS) | Hit Test, Anchors, Depth, Lighting "Shipped" | https://developers.google.com/ar/develop/webxr/requirements, https://developers.google.com/ar/develop/webxr/arcore-comparison |
| **iOS Safari (iPhone/iPad)** | No WebXR | No WebXR | n/a | Use **AR Quick Look (USDZ)**, e.g. via `<model-viewer>` `ar-modes="webxr scene-viewer quick-look"`. It auto-generates USDZ from GLB if `ios-src` is omitted (no animations) (https://modelviewer.dev/docs/, https://modelviewer.dev/docs/faq.html) |
| **HoloLens 2** | — | — | — | **Discontinued:** "As of December 2024, HoloLens devices are no longer manufactured". Nov 2024 was the final feature release. Security updates run "through December 2027", with a UK PSTI minimum of 2028-02-28 (https://learn.microsoft.com/en-us/hololens/hololens-release-notes). WebView2 on HL2 is discontinued. **Do not target it for new work** |

### Native options
| Option | Notes |
|---|---|
| **Cesium for Unity** (Apache-2.0, free) | Streams 3D Tiles on a WGS84 globe. Lists Windows/macOS/Android and Quest 2/Pro VR (https://cesium.com/platform/cesium-for-unity/, https://github.com/CesiumGS/cesium-unity). iOS/visionOS not stated **[UNVERIFIED]** |
| **Cesium for Unreal** (Apache-2.0) | Same idea, heavier toolchain. **[UNVERIFIED platform list]** |
| **ARCore Geospatial API** | **True lat/lon/alt anchoring** (WGS84, terrain, rooftop anchors) using VPS in Street View coverage. Android, iOS, Unity AR Foundation, Unreal. **Not available in WebXR** (https://developers.google.com/ar/develop/geospatial). The only realistic "overlay at real coordinates" path. Needs a Google Cloud project, which may conflict with Gov constraints |
| **OpenXR** | Runtime standard under Unity/Unreal on Quest/PC. Relevant only if going native |

### Practical demoable path
1. **Desktop/web:** globe + 3D Tiles (place the model at its georeference).
2. **Quest Browser:** the same three.js scene enters `immersive-vr` ("walk the site" at 1:1 scale, teleport) and `immersive-ar` (tabletop-scale model on a real table via hit-test/planes in passthrough).
3. **Phones:** a `<model-viewer>` "View in AR" button on the asset page, using the decimated proxy GLB (WebXR on Android, Quick Look on iOS).
4. **Vision Pro:** immersive-vr works in the same web app. AR only via USDZ / `<model>`.
5. True geo-anchored on-site AR → a stretch goal with native ARCore Geospatial (Unity), explicitly out of scope for a solo portfolio MVP.

---

## 6. Recommendation (solo-dev portfolio scope)

### (a) Ingest processing for GLB (containerized worker, e.g. Azure Container Apps Jobs / Batch)
1. **Sniff** (Python or Node): Range-GET 20 bytes + JSON chunk from Blob. Record the header, extensions, counts, bounds and size class. Reject >2^32-4 bytes as non-conformant GLB (or route it to a `.gltf`+bin path).
2. **Validate**: Khronos glTF-Validator (report stored as an artifact).
3. **Proxy**: `gltfpack -i in.glb -o proxy.glb -si 0.05 -c -tc` (native binary; tune the ratio by triangle count) → used for thumbnails, the `<model-viewer>` AR view and quick preview.
4. **Thumbnail/turntable**: headless Chromium + three.js render of the proxy (one renderer codebase shared with the viewer). ffmpeg for MP4.
5. **Tile**: `mago-3d-tiler` (Docker) GLB → 3D Tiles 1.1 (glTF content, implicit tiling optional) with CRS/lon/lat if known. Then `3d-tiles-tools` for validation/`convert` to `.3tz` or a folder in Blob. Fallback/alt: GLB→OBJ→Obj2Tiles (watch AGPL).
6. **Georeference record**: store the ENU origin + HPR (heading, pitch, roll) in the catalog. Write the root `transform`. Expose a "place on map" editor when it is missing.
7. **Point clouds** (adjacent): PDAL → COPC for storage, py3dtiles or mago for 3D Tiles if they should share the same viewer.

### (b) Web viewer
- **Primary: three.js + 3DTilesRendererJS + WebXR**, with an XYZ/WMTS imagery plugin on the ellipsoid (Azure Maps Gov tiles via `atlas.azure.us`, or self-hosted COG tiles), plus three's `VRButton`/`ARButton`. Reason: **one codebase gives desktop, Quest VR and Quest/Android AR.** CesiumJS cannot enter WebXR today **[DATE]**.
- **Optional companion: CesiumJS "GIS mode"** page for analysts (terrain, imagery layers, vector/raster overlays from the other ingest types) loading the **same** 3D Tiles output. This shows format interoperability.
- **Asset detail page:** `<model-viewer>` with the proxy GLB + AR button (Android WebXR / iOS Quick Look).
- Auth: tiles served from Blob behind the API with OIDC tokens. 3DTilesRendererJS/Cesium both allow custom fetch headers **[UNVERIFIED exact API, check `fetchOptions`/`Resource` headers]**.

### Key risks
- Memory for multi-GB transforms: run gltfpack/mago with large RAM workers (16–64 GB). Do not use the glTF-Transform Node path on the raw file.
- Georeference is usually missing from GLBs. That is a product/UX problem, not a tooling one.
- XR browser support is a moving target. Re-verify Quest/visionOS/Chrome before the demo.
- Gov constraints: avoid default Cesium ion/Bing/Google tiles and keys. Self-host imagery and terrain.
