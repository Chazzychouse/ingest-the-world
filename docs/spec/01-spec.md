# Spec — Ingest the World

Status: draft v0.1, 2026-10-24. Requirements: `00-requirements-brief.md`. Stack: `02-stack.md`. Research: `../research/`.

## 1. Purpose

An internal platform that takes in any file a geospatial organization produces (imagery, vectors, point clouds, multi-GB 3D models, tiny sensor and lab readings). It stores each file where it belongs and describes it with a searchable metadata record. Users can then find, preview and use the data on a map, in 3D, and in AR/VR.

## 2. Glossary

| Term | Meaning |
|------|---------|
| **Upload** | A client session that moves bytes into the landing zone. Lives until the blob is committed or abandoned. |
| **Asset** | One stored file (original or derived), with size, checksum, media type, and role. |
| **Item** | The catalog record for one logical dataset. It is a STAC 1.1 Item and owns one or more Assets. |
| **Bundle** | Multiple files that form one dataset (`.shp/.dbf/.prj`, `.gltf + .bin`, zipped File Geodatabase). |
| **Collection** | A project-scoped group of Items, and the unit of access: grants and markings are set per collection. |
| **Derivative** | An optimized copy made from an original: COG, COPC, GeoParquet, Zarr, 3D Tiles, proxy GLB, thumbnail. |
| **Kind** | The classifier's output, e.g. `raster.cog`, `vector.shapefile`, `pointcloud.copc`, `mesh.glb`, `tabular.readings`. |

## 3. Architecture

```
 Browser (React SPA, OIDC PKCE)
   │ 1. POST /uploads  ──────────────►  Platform API (FastAPI) ── Postgres (catalog, uploads)
   │ ◄── per-blob write-only SAS           │  STAC-shaped item + search API (D7)
   │ 2. PUT blocks in parallel ──────►  Blob: landing/
   │ 3. Put Block List (commit)
   ▼
 Event Grid BlobCreated ─► Service Bus `ingest` ─► Sniff/Classify worker
                                                     │ routes by Kind
                     ┌───────────────┬───────────────┼───────────────┬────────────────┐
                     ▼               ▼               ▼               ▼                ▼
               raster/vector    pointcloud        mesh            tabular        generic/doc
               (GDAL)           (PDAL)            (gltfpack,      (Parquet +      (Tika-style
                                                  mago, three)    Timescale)      text/EXIF)
                     └───────────────┴──── upsert Item + Assets ─────┴────────────────┘
                                                     │
                                       outbox ─► embed worker (Azure OpenAI) ─► catalog.embedding
 Read path: SPA ─► Platform search API / STAC API · TiTiler (COG previews) · Martin (footprint tiles)
            · Blob read SAS for 3D Tiles / downloads
```

### Storage layout (one flat-namespace account, versioning on)

| Container | Contents | Mutability | Tier policy |
|-----------|----------|------------|-------------|
| `landing` | In-flight and just-committed uploads | Deleted after promotion | 7-day cleanup of uncommitted/abandoned |
| `raw` | Originals, `raw/{collection}/{item_id}/{filename}` | Immutable (versioned) | Hot → Cool at 30 d → Cold at 180 d (configurable) |
| `derived` | Derivatives, `derived/{collection}/{item_id}/{role}/…` | Rebuildable | Hot |
| `quarantine` | Files that failed validation or scanning | Admin only | Cool |

**Retention (D4).** Each Collection carries a `retention` policy: `retain_days`, a tiering schedule (by age or last access), and `legal_hold` (maps to a Blob immutability policy on the `raw/{collection}/` prefix). Soft delete is enabled on `raw`. Derivatives can be deleted and are regenerated on demand. An Item whose original sits in Archive tier shows a "rehydrate" action instead of download.

## 4. Ingestion pipeline

1. **Upload initiation.** The client sends filename(s), size, an optional client-side checksum, the target collection and optional tags. The API creates an `upload` row and returns a SAS scoped to one blob that expires in ≤ 1 h and can be renewed. A bundle gets one SAS per file under a shared upload prefix.
2. **Transfer.** The SPA uploads blocks in parallel (default 8–32 MiB blocks, 4–8 concurrent; adaptive). Progress is kept in IndexedDB so an upload resumes after a page reload.
3. **Commit → event.** `Put Block List` fires `BlobCreated`. Event Grid forwards it to Service Bus `ingest`. Locally, the event shim does this job.
4. **Sniff and classify.** A range-read of the first ~4 KB and last ~64 KB is matched against magic-byte rules, refined by a ZIP central-directory listing. The result is a Kind plus a confidence score. Bundles wait until all expected members arrive or a timeout passes.
5. **Scan and promote.** The file is malware-scanned (Defender for Storage in cloud, ClamAV locally); failures go to `quarantine`. A server-side copy then moves it `landing → raw`, a full-file SHA-256 is computed while streaming, and the file is deduplicated by checksum within its collection: a file whose checksum matches an item already in the collection is rejected as `duplicate` (its raw copy discarded), not linked to the existing item, so an item always has exactly one upload, one uploader and one raw blob (ING-9). The rejection names nothing about the existing item; the uploader can read the collection and find it. The Item inherits its collection's marking (D3).
6. **Extract.** Header-only extraction runs first, in a child process with a deadline and a memory limit (ING-8), within a byte budget (e.g. COG IFDs, LAS/COPC header, GLB JSON chunk, Parquet footer, GeoPackage's `gpkg_contents` and `gpkg_spatial_ref_sys` pages, a Shapefile's 100-byte header; GeoJSON has no header, so it is read whole only when it fits the budget). The fallback is a streaming read. Output is normalized to a STAC Item:
   - `geometry` is the WGS84 footprint, or `null` for non-spatial data
   - `proj:*` holds the native CRS
   - `file:*` holds size and checksum
   - per-Kind extension fields: `raster:`, `pointcloud:`, our own `mesh:` and `reading:`
7. **Derive (async, per Kind).**
   - raster → COG plus a thumbnail
   - vector → GeoParquet; the footprint goes to Martin tiles
   - point cloud → COPC, and optionally 3D Tiles
   - mesh → **normalize** (D1: FBX/OBJ via Assimp or headless Blender, IFC via IfcOpenShell, USD via usd-core → glTF; glTF + external buffers passes through, with no 4 GiB limit), validate, gltfpack a proxy GLB, render a headless thumbnail, then run mago-3d-tiler to make 3D Tiles 1.1. Converters are plugins behind one `MeshNormalizer` interface.
   - tabular readings → per-device-per-day Parquet, plus TimescaleDB rows
   
   Each derivative is an Asset with STAC `processing:` lineage.
8. **Index.** The Item row in `catalog.items` is the search row: it is written once, in the same transaction as the outcome and its audit event, and an outbox event is written. The embed worker builds a text description and stores its embedding.
9. **Notify.** Status updates stream to the UI over SSE: `uploading → queued → classifying → extracting → deriving → ready | failed`.

### 4.1 Streaming readings (D2)

`POST /readings` takes a batch of JSON Lines or CSV rows (≤ 5 MB per request), authenticated with a device or service identity. It validates each row against the collection's reading schema and inserts into the `readings.measurements` hypertable. A nightly compaction job writes per-device-per-day Parquet to `raw/` and upserts one STAC Item for each. File-based readings go through the same compaction output, so search sees one model either way. An MQTT / Event Hubs adapter is a later plugin that posts to the same service.

**Idempotency.** Every stage is keyed on `(blob_url, etag, stage)`, so repeat deliveries are no-ops. A failed attempt backs off before its message is released for redelivery, and the message's lock is renewed while a job runs (ING-10). On the 5th and last delivery a version that still fails is rejected as `retries-exhausted`; a message that dead-letters anyway is swept by the worker, which rejects its version as `dead-lettered` (ING-2). Either way the job keeps its last error for the admin UI, and the uploader uploads again.

**Size classes.** Workers are scheduled by size: S (< 100 MB), M (< 5 GB) and L (≥ 5 GB, mesh). Each class gets its own queue subscription and resource profile. An L mesh job requests 32–64 GB RAM and local NVMe scratch.

## 5. Data model (Postgres)

- `catalog.items` — one row per cataloged file, and the source of truth for search (D7): `id` (the upload's), `collection_id`, `kind`, `media_type`, `filename`, `tags text[]`, `marking`, `size_bytes`, `sha256`, `raw_blob`, `created_by`, `created_at`, header metadata (`extraction`, `crs`, `footprint geometry(Polygon,4326)`, `properties jsonb` with STAC extension fields), and `search_text`, generated from filename and tags.
  - Indexes: GIST(footprint), GIN(search_text gin_trgm_ops), btree on (created_at, id) for keyset paging and on (collection_id, created_at).
  - `captured tstzrange`, when the header says when the data was captured, and `datetime_range`, generated from it or, failing that, from `created_at` (GiST-indexed, M2).
  - Later: `title`/`description` once items can be edited, `fts tsvector` over extracted text and `embedding vector(1536)` (M5), each with its index.
- `ingest.uploads`, `ingest.jobs` (stage, attempts, error, timings), `ingest.outbox`.
- `readings.measurements` — a TimescaleDB hypertable `(device_id, ts, metric, value, item_id)`.
- `authz.collection_grants` — collection → role/group grants. Items inherit their collection's grant.
- `authz.markings` — marking levels (`UNCLASSIFIED`, `CUI`), CUI categories and dissemination controls. `collections.marking` is required. `items.marking` must be ≥ the collection marking (CHECK + trigger).
- `audit.events` — append-only: actor, action, target, marking, ip, at.

## 6. Search

`GET /items/{id}` returns one Item and `POST /search` searches them. Both speak STAC: an Item is a STAC 1.1 Item (GeoJSON Feature, `proj:`/`raster:`/`pc:`/`table:` and our `mesh:` fields in `properties`), and a search returns an ItemCollection whose `next` link carries the cursor. Item ids are UUIDs; an unreadable item answers exactly like a missing one (ACC-2).

`POST /search` (M1) takes any combination of:

- **Spatial:** `bbox` (`[west, south, east, north]`; `west > east` crosses the antimeridian) or `intersects` (a GeoJSON Point, Polygon or MultiPolygon, at most 10,000 positions), never both. Items with no footprint match neither.
- **Temporal:** `datetime`, an RFC 3339 instant or a closed or half-open interval (`start/end`, `..` for open), matched by overlap against the item's time: when it was captured (a TIFF/EXIF/imagery tag, or a LAS file's creation day) or, when its header doesn't say, the instant it was cataloged. That is also what its STAC `datetime` shows, or `start_datetime`/`end_datetime` for a range. `created` takes the same forms and matches when it was cataloged.
- **Text:** `q`, whitespace-separated terms, each of which must appear (case-insensitively, as a substring) in the filename or a tag. Substrings rather than words because filenames are written as `site-alpha_ortho_2024.tif`, which a word tokenizer keeps as one token; served by a trigram index.
- **Facets:** `collections`, `kinds`, `ids`.
- **Paging:** `limit` (1–100, default 20) and the `token` from the previous page's `next` link. Results are ordered newest cataloged first by `(created_at, id)`, a total order, so the cursor is a position in it and paging is keyset, never offset: every item that matched when paging began is returned exactly once (SRCH-2), however many items arrive meanwhile.

Every query filters through `authz.readable_scopes` (ACC-1), and each page is one audited request (ACC-5).

Later: relevance and size sorting, facet counts, `within_distance`, fuzzy title matching with `pg_trgm` similarity, semantic search fused with keyword ranking by RRF (M5), and a conformant STAC API at `/stac` (landing page, conformance classes, `GET /search`, CQL2) for QGIS, pystac-client and STAC Browser.

## 7. Auth and authorization

- OIDC only. The SPA uses auth code + PKCE. The API validates JWTs against discovered JWKS. Issuer, audience and authority host are configuration, so the same build runs against commercial Entra, Gov Entra (`login.microsoftonline.us`) or local Keycloak.
- Roles (Entra app roles, a `roles` claim): `Reader`, `Contributor` (upload, edit own items), `Curator` (edit any item in granted collections), `Admin`.
- Access is **attribute-based** (D3): a user sees an Item only if they hold a grant on its collection *and* their clearance attribute covers the Item's marking. Clearance comes from an app role or a directory attribute claim. This is enforced in SQL on every item read and search, by filtering through `authz.readable_scopes`, never in the UI.
- The UI shows a marking banner for the highest marking on screen. Downloads and exports record the marking in the audit log.
- Services use Managed Identity, and blob access uses user-delegation SAS. Shared-key access is disabled on the storage account.
- Every upload, download, metadata edit and grant change goes to an audit log table, mirrored to Azure Monitor.

## 8. Visualization and XR

| Surface | Tech | Notes |
|---------|------|-------|
| Catalog map | MapLibre GL + Martin footprint tiles + TiTiler raster previews | Search results stay in sync with the map viewport |
| Item page | Metadata panel, thumbnail, `<model-viewer>` for meshes, raster preview | iOS AR via generated USDZ (stretch) |
| 3D viewer | three.js + 3DTilesRendererJS, on a base-map tile layer | Streams the mesh's 3D Tiles from Blob via read SAS |
| VR | WebXR `immersive-vr` (Quest Browser, Vision Pro Safari) | Walk the site at 1:1 or tabletop scale |
| AR | WebXR `immersive-ar` + hit-test + anchors (Quest passthrough, Android Chrome) | Place the model on a real surface at 1:1 |
| Georeferencing | "Place on map" tool: pick an origin on the map, set heading and scale. The result is saved as the 3D Tiles root transform and in `mesh:origin` | GLB has no CRS, so most models need this step |

**Device targets (D5):** Quest 3 is primary (VR + passthrough AR), then Android Chrome (AR), then Vision Pro (VR only), with iOS/iPadOS AR through Quick Look (USDZ) and desktop as the fallback. Features are switched on by `navigator.xr.isSessionSupported()` and requested features, not by user-agent sniffing.

True lat/lon world anchoring (ARCore Geospatial, native Unity) is out of scope for v1 and listed as a stretch goal.

## 9. Non-functional targets (initial)

| Target | Value |
|--------|-------|
| Max single file | 4 GiB GLB (format limit); 1 TiB other (configurable, Blob allows ~190 TiB) |
| Time to searchable (header-only metadata) | < 60 s after commit for files of any size |
| Concurrent uploads | 50 users × 4 files without API saturation (API never touches the bytes) |
| Search latency | p95 < 300 ms for filtered text + spatial at 10M items |
| Availability | Single region for v1; Gov Virginia for zone-redundant HA |

## 10. Milestones

| M | Scope | Proves |
|---|-------|--------|
| M0 | Repo, Compose stack with seeded sample data, Terraform skeleton (commercial + Gov `plan` in CI), OIDC login with Keycloak, audit log | Local parity, OIDC-generic auth, Gov portability (D6) |
| M0.5 | Invariant catalog (`03-invariants.md`), reference model, stateful simulator, fuzzing, ports + fault-injecting fakes + contract tests, planted-bug runner | Guarantees are explicit and machine-checked before the pipeline exists |
| M1 | Direct-to-blob resumable upload → event → classify → header metadata → search by text/facets. Workers are resumable step machines over the ports; the simulator gains upload, worker and faulty-queue actors (ACC-7, ACC-8, ING-1 to ING-6, SRCH-1, SRCH-2) | Core pipeline (spikes S2, S4) |
| M2 | Spatial and temporal search, map UI, raster/vector derivatives, TiTiler previews | Geospatial value |
| M3 | Mesh pipeline: normalizers (FBX/OBJ/IFC/USD), validation, proxy, thumbnail, 3D Tiles; 3D viewer with "place on map" | Big-file 3D (S1) |
| M4 | WebXR VR + AR on Quest/Android; `<model-viewer>` + USDZ on item pages | Real-world XR (S5) |
| M5 | Readings (streaming endpoint, Timescale, compaction to Parquet), semantic/hybrid search, markings + ABAC, retention policies, live commercial-Azure deployment | Breadth + Gov story (S3) |

## 11. Decisions

D1–D6 are recorded in `00-requirements-brief.md` § Resolved questions and are reflected throughout this spec.

**D7 — STAC is the contract; `catalog.items` is the store (M1 slice 5).** The stack first chose pgSTAC as the system of record with stac-fastapi in front of it. We serve STAC-shaped Items and searches from our own `catalog.items` table instead, because:

- **One writer, one row.** pgSTAC beside `catalog.items` means every item is written twice, in two shapes, by two schemas' rules, and kept in step forever. Duplicated state is a defect here (AGENTS.md), and the pipeline's guarantees (ING-1, ING-2, ING-9) are proved against one table in one transaction.
- **Authorization lives in one SQL function.** ACC-1 requires every read to filter through the database's access rule. pgSTAC builds its own dynamic SQL from CQL2 and registers searches for the tiler to replay; making it honour `authz.readable_scopes` means injecting a predicate into, or row-level security under, code we don't own, and re-proving ACC-1 and ACC-2 after every upgrade.
- **Spike S3 stays a spike.** pgSTAC on Azure Flexible Server without superuser is unverified (S3), and was on M1's critical path only because pgSTAC was. Our schema uses only extensions already allow-listed (PostGIS, pg_trgm, btree_gist).
- **pgSTAC's free text is unindexed** (research 03 §2.1), so search needed our own table anyway.

What we give up, and how it comes back: CQL2 and the stac-fastapi extensions (we add the filters we need, and a conformant `/stac` façade later), and titiler-pgstac's registered-search mosaics (M2). If M2 needs those, pgSTAC becomes a **derived read model**: fed from the outbox, reached only through searches the API has already filtered by `authz.readable_scopes`, never asked an access question itself, and rebuildable like any derivative (ING-7). S3 is then spiked for M2, not M1.

