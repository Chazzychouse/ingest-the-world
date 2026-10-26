# Stack — Ingest the World

Status: proposed, 2026-10-24. Each choice links to the research that justifies it. Items marked **SPIKE** must be proven before the dependent milestone.

## Summary

| Layer | Choice | Why (research ref) |
|-------|--------|--------------------|
| Object storage | Azure Blob, **flat namespace** (not ADLS Gen2), versioning on | HNS is irreversible and drops versioning/change feed/PITR; Azurite can't emulate it (02 §3) |
| Upload path | Browser → Blob directly via short-lived, single-blob, write-only **user-delegation SAS**; parallel block upload; resume state in IndexedDB | 4000 MiB blocks × 50k blocks; uncommitted blocks live 7 days; tus would re-introduce a proxy (02 §2) |
| Eventing | Event Grid `BlobCreated` (filtered to commit APIs) → **Service Bus** queue, dead-lettering on | At-least-once, unordered → idempotent workers keyed on blob URL + eTag (02 §3) |
| Workers | Plain OCI containers pulling from the queue; **Container Apps jobs** (dev/IL2) or **AKS + KEDA** (IL4/5) — same image | Container Apps tops out at IL2 in Gov; Functions too small for multi-GB (4 GB RAM / 0.8 GB disk) (02 §3, §1) |
| Worker language | **Python 3.12**; GDAL via rasterio's wheels (pinned in `uv.lock`), pyarrow for Parquet, pyogrio for vector derivatives (its wheels bundle their own GDAL, so the worker carries two; `--check` loads both in the render sandbox); PDAL and pystac join as derive steps need them; Node/native sidecars for mesh tooling | GDAL/PDAL ecosystem is Python-first (01 §4) |
| Header reads (ING-6) | GDAL and Arrow read through the **blob store port** (rasterio's Python-filesystem opener), never `/vsiaz/` | One authenticated path to storage (managed identity, Gov authority host); every byte counts against the extraction budget and is testable with a fake store |
| Mesh tooling | **Our own tiler** (numpy: streamed partition, vertex-clustering levels of detail, `.3tz` archive) for 3D Tiles 1.1, proxies and thumbnails; Khronos glTF-Validator and Cesium 3d-tiles-validator check its output in tests | mago-3d-tiler failed spike S1: no geometric levels of detail outside a single-threaded mode, memory 10–12× the input (M3 plan) |
| Mesh normalizers (D1) | **Assimp** through `assimp-py` (MIT; wheels bundle Assimp 6, BSD-3) for OBJ, FBX, PLY and STL, in the render sandbox; glTF and GLB read directly; IfcOpenShell (IFC) and usd-core (USD) deferred | One converter for the common interchange formats (M3 plan D-M3-3) |
| Malware scan (D3) | Defender for Storage malware scanning (cloud), ClamAV container (local) | Gov availability of on-upload scanning is **unverified**; ClamAV worker is the fallback |
| Database | **PostgreSQL 16+ (Azure Flexible Server)** + PostGIS, pgvector, pg_trgm, btree_gist, pg_partman, TimescaleDB | All listed extensions supported; available in Gov AZ/TX/VA (02 §1, 03 §3) |
| Catalog | **STAC-shaped API over our own `catalog.items`** (spec D7); pgSTAC 0.9.x only as a derived read model if titiler-pgstac mosaics need it (M2) | One writer and one access rule for every item; pgSTAC's free text is unindexed and S3 is unverified (03 §1–2) |
| Search | The `catalog` schema: facet columns, substring search over a `pg_trgm` index (M1); later `tsvector` FTS over extracted text, `pg_trgm` fuzzy matching, **pgvector HNSW** embeddings, keyword + semantic results merged by **reciprocal rank fusion** in SQL | pgSTAC's own free-text search is unindexed; Azure AI Search can't index polygon footprints (03 §3–4) |
| Embeddings | Azure OpenAI `text-embedding-3-small` (1536-d), behind a provider interface; keyword-only fallback locally | Available in Gov; under pgvector's 2000-d HNSW limit (03 §4) |
| Small readings | Batched to per-device-per-day **Parquet** (one STAC Item each) + TimescaleDB hypertable for queryable readings | Avoid one catalog row per reading (01 §3, 02 §4) |
| Platform API | **Python FastAPI**; serves the STAC-shaped item and search API; OIDC JWT validation against discovered JWKS | One language with workers and stac-fastapi |
| Auth | Generic OIDC (auth code + PKCE in the SPA); **Entra ID app roles** for RBAC; authority host + tenant are config (`login.microsoftonline.com` / `.us`); Managed Identity service-to-service | Gov apps can sign in commercial or Gov tenants; groups claim overflows past 200 groups (02 §5) |
| Map tiles / previews | **TiTiler-pgstac** (COG previews/mosaics), **Martin** or **tipg** (footprint vector tiles) | eoAPI / Planetary Computer pattern (03 §6) |
| Web UI | **React + TypeScript + Vite** SPA; **MapLibre GL** for catalog browsing; **three.js + 3DTilesRendererJS** with **WebXR** for the 3D/XR viewer; `<model-viewer>` for phone AR (USDZ Quick Look on iOS); optional CesiumJS page | CesiumJS can't enter WebXR; Quest Browser is the strongest XR target (04 §4–5) |
| Base map | Azure Maps (Gov: `atlas.azure.us`) or self-hosted imagery; pluggable | (04 §4) |
| IaC | **Terraform** (`azurerm` with `environment = "usgovernment"`, `azuread` for app registrations + app roles) | One tool covers infra *and* Entra config (02 §7) |
| Local dev | Docker Compose: Azurite, Postgres+PostGIS+pgvector, Service Bus emulator, a small **event shim** that forwards Azurite commits to the queue, Keycloak as a stand-in OIDC IdP | No official Event Grid emulator; Keycloak proves the OIDC-generic claim (02 §6) |
| Observability | OpenTelemetry → Azure Monitor / App Insights (local: Grafana LGTM container) | |

## Explicit non-choices

- **Azure AI Search as the primary index** — it indexes points only. Revisit only as an add-on for its semantic ranker.
- **OpenSearch/Elasticsearch** — not managed in Gov (unconfirmed); deferred. The outbox table (spec §6) keeps this option open.
- **Azure Functions for processing** — fine for tiny glue code, wrong for multi-GB files.
- **HoloLens 2** — production ended Dec 2024; don't target it.
- **Cesium ion** — commercial and not confirmed for Gov. mago-3d-tiler did fail S1; our own tiler passed it instead.
- **mago-3d-tiler** — failed S1 (M3 plan): a 1.6 GB GLB ran out of 13 GiB, and its levels of detail keep every triangle.
- **Vercel / non-Azure hosting** — the client constraint is Azure (Gov).
- **Effect (v4) across the web app** — the SPA is a thin view over generated API types, and Effect's runtime and idiom cost more than they save there. Planned instead for the M1 resumable upload engine alone, where retries with backoff, bounded concurrency, cancellation and typed errors are the whole job.

## Spikes (prove before building on them)

| # | Spike | Pass criteria |
|---|-------|---------------|
| S1 | ~~mago-3d-tiler~~ A tiler on a 2–4 GB GLB | Valid 3D Tiles 1.1 (Cesium 3d-tiles-validator; 3d-tiles-tools has no validator), streams in 3DTilesRendererJS, peak RAM recorded. mago failed; our own tiler passed on 3.03 GB in 3.4 min and 572 MiB (M3 plan) |
| S2 | Browser block upload of a 10 GB file to Azurite and to real Blob, with a mid-upload reload | Resumes from IndexedDB state; commit triggers exactly one processed record |
| S3 | pgSTAC 0.9.x install on Azure Flexible Server (needed only if M2 adds pgSTAC as a derived read model, spec D7) | Migrations run without superuser-only steps |
| S4 | ~~GDAL `/vsiaz/` against a Gov-style endpoint with an externally supplied token~~ Replaced in M1 slice 4: GDAL reads through the blob store port, so it needs no endpoint or token of its own | Range-only reads proven locally (a 64 MiB tiled GeoTIFF in one 64 KiB request); the Gov endpoint itself is the blob adapter's, verified with it |
| S5 | WebXR on Quest Browser: tiled model in VR + passthrough AR hit-test placement | 72 fps on a mid-size tileset |
