# Ingest the World

An internal platform for ingesting geospatial data of every shape — tiny sensor readings,
gigabyte rasters and point clouds, multi-GB GLB models — routing each file to the right
store, extracting searchable metadata, and viewing it on a map, in 3D and in AR/VR.
Built Azure-first and Azure Government-ready, with provider-neutral OIDC.

- Spec: [`docs/spec/01-spec.md`](docs/spec/01-spec.md) · Stack: [`docs/spec/02-stack.md`](docs/spec/02-stack.md) · Decisions D1–D6: [`docs/spec/00-requirements-brief.md`](docs/spec/00-requirements-brief.md)
- Research (primary-source): [`docs/research/`](docs/research/)

## Run it locally

Requires Docker. No Azure account needed.

```sh
cp .env.example .env   # set ACCEPT_EULA=Y to add the queue (see below)
make up
make demo              # optional: load the demo catalog, real files (~1 GB, fetched once)
open http://localhost:8081
```

The ingest queue runs on the Service Bus emulator, which with its SQL Server is licensed under
Microsoft's EULAs. `ACCEPT_EULA=Y` in `.env` accepts them and adds three services to the stack: the emulator;
the event shim, which announces finished uploads on the queue, standing in for Event Grid; and
the worker, which scans each upload, promotes it to `raw/` and catalogs it. It also adds the
emulator tests to `make check`. Without it everything else runs and the queue tests skip.

| User  | Password | Roles | Clearance | Sees |
|-------|----------|-------|-----------|------|
| alice | alice | Admin | CUI | everything |
| carol | carol | Contributor | CUI | all seeded collections |
| bob   | bob   | Reader | UNCLASSIFIED | unclassified collections only |
| e2e   | e2e   | Contributor | CUI | only the collections `make e2e` makes, granted to it by name |

Development credentials only, defined in [`infra/keycloak/ingest-realm.json`](infra/keycloak/ingest-realm.json).
Keycloak imports the realm only when it first starts, so a changed realm file needs `make reset`.

The demo catalog ([`samples/demo.json`](samples/demo.json)) is public data uploaded through the
pipeline: Sentinel-2 scenes of the Outer Banks, the Mississippi delta, San Francisco Bay and Maui
after the Lahaina fire; Copernicus elevation tiles either side of the antimeridian and near both
poles; and the sample corpus's point clouds, meshes, vectors and tables. `make demo` uploads only
what isn't there yet; `make demo-reset` starts from empty. The end-to-end suite's collections are
granted to the `e2e` user alone, so carol and bob never see them; alice, an Admin, sees every
collection by design.

| Service | URL |
|---------|-----|
| Web (nginx, built SPA) | http://localhost:8081 |
| API (OpenAPI docs at `/docs`) | http://localhost:8000 |
| Tiler (raster previews, under a token the API mints) | http://localhost:8001 |
| Keycloak (admin / admin) | http://localhost:8080 |
| Postgres + PostGIS + pgvector | `postgresql://ingest:ingest@localhost:5432/ingest` |
| Azurite (Blob emulator) | http://localhost:10000 |
| Test Postgres and Azurite (`make services`, in memory, `compose.test.yaml`) | `localhost:5433`, http://localhost:10010 |
| Service Bus emulator (with `ACCEPT_EULA=Y`) | `amqp://localhost:5672`, health at http://localhost:5300/health |

For hot reload, run `cd web && pnpm dev` (http://localhost:5173, proxies `/api` to :8000).

## Repository layout

| Path | What |
|------|------|
| `api/` | Platform API — FastAPI, OIDC verification, attribute-based access, audit log, SQL migrations |
| `web/` | React + TypeScript SPA — OIDC (auth code + PKCE), marking banners |
| `infra/terraform/` | One module for commercial Azure and Azure Government (`cloud = "public" \| "usgovernment"`) |
| `infra/keycloak/` | Local IdP realm that issues Entra-shaped tokens |
| `infra/docker/postgres/` | PostGIS + pgvector image matching Azure Flexible Server's extensions |
| `docs/` | Research, spec, stack, developer guides |

## Invariants and the test harness

The platform's guarantees are written down as invariants with stable IDs in
[`docs/spec/03-invariants.md`](docs/spec/03-invariants.md) (no leak, non-disclosure, audit
coverage, idempotent ingest, ...). Every check in the suite names the invariant it enforces,
and the build fails if an `enforced` invariant has no check.

| Layer | What it does |
|---|---|
| Reference model (`api/tests/model.py`) | Independent definition of who may read what; the SQL predicate is property-tested against it |
| Simulator (`api/tests/sim/`) | Hypothesis state machine: random actors create, read, grant and revoke through the real API and Postgres; invariants checked after every step; failures shrink to a minimal replayable sequence |
| Fuzzing (`api/tests/test_fuzz.py`, `test_robustness.py`) | Schema-driven requests to every route as three users, plus targeted hostile text: no 5xx, no CUI to uncleared users |
| Ports and fakes (`api/ingest_api/ports.py`, `api/tests/fakes.py`) | Clock, ids, blob store and queue behind interfaces; seeded fakes inject faults (duplicates, reordering, lost acks, transient errors) |
| Contract tests (`api/tests/contracts/`) | Each fake passes the same suite as the real thing (Azurite, the Service Bus emulator), so the simulator can't drift from reality |
| Planted bugs (`api/tools/mutants.py`) | Injects known bugs one at a time and checks each is caught, by the right invariant |

```sh
make check        # the gate: lint + all tests (derandomized, as CI) + traceability + JSON report
make explore      # property tests and simulator with fresh random inputs
make sim SEED=7   # replay a simulator run
make soak         # long simulator run
make mutants      # planted-bug run
make tf-validate  # Terraform fmt + validate, via the official image
```

`make check` writes `api/reports/invariants.json` (each invariant's result, and per failure
the invariants broken, the error and a repro command) for agents and CI. CI runs `check`,
the web tests and Terraform validation on every push; soak and planted bugs run nightly.
Terraform `plan` runs per cloud when the repository defines `AZURE_CLIENT_ID_{PUBLIC,GOV}`
etc. as variables (GitHub OIDC federation, no secrets). Code standards (duplication
rule, Power of 10, typing) and agent guidance are in
[`AGENTS.md`](AGENTS.md).

## Status

**M0 — foundation**: local stack, OIDC login, marking-aware access control,
append-only audit log, Terraform for storage / Service Bus / Event Grid / Postgres / Entra.
Compute (Container Apps jobs or AKS) arrives with the M1 workers. See the spec for M1–M5.

**M0.5 — invariants and harness**: the invariant catalog, reference model, simulator,
fuzzing, ports with fault-injecting fakes and contract tests, planted-bug runner, and
collection grant management (`/collections/{id}/grants`).

**M1 — pipeline**: the browser uploads straight to Blob Storage in resumable blocks, through
a signed URL for exactly one landing blob (ACC-8) that only someone with write access can get
(ACC-7). A queue event starts the worker's step machine: scan, promote, hash and deduplicate,
classify, and read header metadata in a sandbox under a budget (ING-1 to ING-6, ING-9,
ING-10). Cataloged files are STAC Items, searchable by text, kind, collection, time and area
with complete keyset paging (SRCH-1, SRCH-2), and shown on a map. The Upload tab lists your
uploads from the API (`GET /uploads`), says where each is in the pipeline, explains every
rejection, and resumes an unfinished one from any browser.

**M2 — geospatial value**: search by when data was captured (read from each file's header) as
well as when it was uploaded, and footprints for GeoTIFF/COG, LAS/LAZ, GeoParquet, GeoPackage,
Shapefile and GeoJSON (ING-6). A separate derive stage, fed through an outbox, makes a COG and
a thumbnail of each raster and a GeoParquet copy of each vector file in a sandbox; derivatives are rebuildable, never change an outcome,
and exist once per role and recipe (ING-7, ING-11, ING-12). Every byte path asks the same
access question (ACC-9): downloads are audited redirects to a read URL for one blob that expires
within 15 minutes (ACC-8); the map draws every match from footprint vector tiles and previews a
raster from our own tiler, each under an audited session (ACC-5). Uploads report live over
server-sent events until their derivatives are ready (ING-13). The plan, and where each slice
departed from it, is [`docs/plans/m2.md`](docs/plans/m2.md).
