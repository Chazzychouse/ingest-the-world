# Requirements Brief — Ingest the World

Status: draft, captured 2026-10-24 from the initial project conversation. Feeds `docs/spec/01-spec.md` once research lands.

## Context

Portfolio project targeting a geospatial technology employer. The client profile: a government contractor that runs heavily on Azure and needs an internal tool to ingest, organize, and find data.

## Functional requirements

| ID | Requirement | Source |
|----|-------------|--------|
| F1 | Accept a wide range of file types (raster, vector, point cloud, 3D models, tabular/sensor, documents) | stated |
| F2 | Classify each file and route it to the correct storage: object/blob storage, relational tables, or derived/optimized stores | stated |
| F3 | Extract and store metadata for every file | stated |
| F4 | Metadata is highly searchable: full-text, faceted, spatial, temporal | stated (expected) |
| F5 | Geospatial-native: CRS, footprints, map-based browsing | stated (desired) |
| F6 | AR/VR viewing that is useful in the real world, e.g. georeferenced models, on-site overlay | stated (desired) |
| F7 | OIDC authentication; Microsoft Entra ID (incl. Azure Government) is the primary IdP | stated |

## Non-functional requirements

| ID | Requirement | Notes |
|----|-------------|-------|
| N1 | Moderate-to-heavy internal throughput | concurrent uploads, background processing must scale out |
| N2 | File size range: bytes (IoT / biological test readings) → ~1 GB typical → multi-GB GLB models | uploads must be resumable and must not pass through the API server |
| N3 | Azure-first, Azure Government compatible | endpoints, service availability, FedRAMP posture |
| N4 | Runs locally for development and demos without cloud spend | emulators / Docker |

## Resolved questions (decided 2026-10-24)

The user asked for options that show flexibility and an understanding of the problem. Where the real answer is "it depends", each decision supports more than one case behind a single internal model.

| # | Question | Decision | Why it showcases the problem |
|---|----------|----------|------------------------------|
| D1 | GLB size ceiling | Accept GLB ≤ 4 GiB, **glTF + external buffers of any size**, and **FBX / OBJ / IFC / USD(Z)** through converter plugins that normalize to glTF. 3D Tiles is the canonical delivery format. | Shows the GLB uint32 limit is understood and that the pipeline isn't tied to one vendor's format |
| D2 | Readings: files or a stream | **Both, converging on one model.** Files (CSV / JSON Lines / Parquet) use the normal pipeline. A streaming endpoint (HTTPS batch POST in v1; MQTT / Event Hubs adapter later) writes to TimescaleDB. A nightly compaction job rolls the readings into per-device-per-day Parquet and one STAC Item each. | Hot rows can be queried, the catalog stays small, and the warm archive is cheap |
| D3 | Sensitivity | **CUI-ready, not classified.** Every Collection carries a marking (`UNCLASSIFIED`, `CUI` + categories / dissemination controls). An Item can raise but never lower its collection's marking. Access is attribute-based (role + marking clearance + collection grant) and enforced in SQL. Audit log from day one. Malware scan before promotion (Defender for Storage in cloud, ClamAV locally). UI shows marking banners. | Shows awareness of gov data handling without claiming an accreditation |
| D4 | Retention | Originals are **immutable and versioned**, with soft delete. Derivatives are **disposable and rebuildable**. Each collection has a policy: retention days, tiering (Hot → Cool → Cold → Archive by age / last access), and optional **legal hold** via Blob immutability policies. Archive-tier items show "rehydrate on request". | Separates the record of truth from caches and treats cost as part of the design |
| D5 | XR hardware | **Meta Quest 3** is primary (VR + passthrough AR), then **Android Chrome** AR, then **Vision Pro** VR. **iOS/iPadOS** uses Quick Look via USDZ. Desktop is always the fallback. Native Unity + ARCore Geospatial is a stretch goal for true lat/lon anchoring. HoloLens 2 is excluded. | One WebXR codebase, with progressive enhancement by device capability |
| D6 | Demo hosting | **Three tiers.** (1) `docker compose up` local demo with seeded sample data; this is the primary portfolio artifact. (2) Terraform that deploys to commercial Azure with scale-to-zero, for a live demo when needed. (3) The same Terraform with `cloud = "usgovernment"`; CI runs `validate`/`plan` against both clouds. | Proves Gov portability without needing a Gov subscription |

## Research index

- `docs/research/01-file-formats-and-metadata.md` — formats, detection, extraction, STAC
- `docs/research/02-azure-platform-and-auth.md` — Azure Gov services, large uploads, eventing, OIDC
- `docs/research/03-metadata-catalog-and-search.md` — STAC API, pgSTAC, search engines, semantic search
- `docs/research/04-3d-and-xr-visualization.md` — GLB handling, 3D Tiles, viewers, WebXR
