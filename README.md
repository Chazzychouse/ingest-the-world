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

## Checks

```sh
make test         # API unit + integration tests (spins up Compose Postgres)
make lint         # ruff + oxlint
make tf-validate  # Terraform fmt + validate, via the official image
```

CI runs all three. Terraform `plan` runs per cloud when the repository defines
`AZURE_CLIENT_ID_{PUBLIC,GOV}` etc. as variables (GitHub OIDC federation, no secrets).

## Status

**M0 — foundation** (this branch): local stack, OIDC login, marking-aware access control,
append-only audit log, Terraform for storage / Service Bus / Event Grid / Postgres / Entra.
Compute (Container Apps jobs or AKS) arrives with the M1 workers. See the spec for M1–M5.
