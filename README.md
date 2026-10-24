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
make up          # or: docker compose up -d --build --wait
open http://localhost:8081
```

| User  | Password | Roles | Clearance | Sees |
|-------|----------|-------|-----------|------|
| alice | alice | Admin | CUI | everything |
| carol | carol | Contributor | CUI | all seeded collections |
| bob   | bob   | Reader | UNCLASSIFIED | unclassified collections only |

Development credentials only, defined in [`infra/keycloak/ingest-realm.json`](infra/keycloak/ingest-realm.json).

| Service | URL |
|---------|-----|
| Web (nginx, built SPA) | http://localhost:8081 |
| API (OpenAPI docs at `/docs`) | http://localhost:8000 |
| Keycloak (admin / admin) | http://localhost:8080 |
| Postgres + PostGIS + pgvector | `postgresql://ingest:ingest@localhost:5432/ingest` |
| Azurite (Blob emulator) | http://localhost:10000 |

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
| Contract tests (`api/tests/contracts/`) | Each fake passes the same suite as the real thing (Azurite), so the simulator can't drift from reality |
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

**M0 — foundation** (this branch): local stack, OIDC login, marking-aware access control,
append-only audit log, Terraform for storage / Service Bus / Event Grid / Postgres / Entra.
Compute (Container Apps jobs or AKS) arrives with the M1 workers. See the spec for M1–M5.

**M0.5 — invariants and harness**: the invariant catalog, reference model, simulator,
fuzzing, ports with fault-injecting fakes and contract tests, planted-bug runner, and
collection grant management (`/collections/{id}/grants`).
