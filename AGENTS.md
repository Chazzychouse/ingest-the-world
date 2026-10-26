# Working in this repo

Guidance for AI agents and humans alike. `CLAUDE.md` imports this file. Read the whole thing
before changing code; the standards below are review criteria, not suggestions.

## The contract: invariants

`docs/spec/03-invariants.md` lists the properties the platform must always hold (ACC-1 no
leak, ACC-5 audit coverage, ING-1 idempotent processing, and so on), each with a stable ID.
It is the most important document in the repo.

- **Never weaken a check to make it pass.** If an invariant looks wrong, change it in the
  doc with the reason in the commit message, then change the check.
- **New behaviour names its invariants.** Tag tests with `@pytest.mark.invariant("ID")`,
  and raise failures with `tests.invariants.check("ID", condition, message)` so they report
  which property broke. A feature that needs a new property adds it to the doc first.
- **A fixed bug the suite missed becomes a mutant** in `api/tools/mutants.py`, so the
  harness proves it would catch that bug next time.

## Commands

All from the repo root. Postgres, Azurite and (once its licence is accepted in `.env`, see
`.env.example`) the Service Bus emulator come up automatically.

| Command | Use it when |
|---|---|
| `make check` | Before saying any work is done. Lint, type check, all tests (derandomized, same as CI), invariant traceability, and `api/reports/invariants.json`. |
| `make explore` | After touching authorization, audit or the pipeline: property tests and the simulator with fresh random inputs. |
| `make sim SEED=n` | Replaying a simulator failure from its seed. |
| `make soak` | Long simulator run before a milestone. |
| `make mutants` | After changing tests or checks: plants known bugs and verifies each is caught by the right invariant (runs only the tests declaring each mutant's invariants; ~5 min). |
| `make mutants-full` | The same against the whole suite per mutant (~40 min); nightly CI runs it. |
| `make up` | Running the full local stack (http://localhost:8081). |
| `make samples` | Fetching and verifying the sample corpus (`samples/manifest.json`); `LARGE=1` adds multi-GB files. |
| `make e2e` | After `make up`, when a change touches the pipeline end to end: uploads every sample through the stack and checks its outcome and metadata. |
| `make image-smoke` | After changing the API image or a native dependency: builds it and runs the worker's `--check`. |

## Application structure

Keep it boring: a request goes **route → rules → store → database**, and nothing points back
up that chain.

```
api/ingest_api/
  app.py         composition root: builds the app, opens the pool, registers routers
  settings.py    all configuration, from INGEST_* environment variables
  deps.py        request-scoped dependencies: connection, principal, auditor, role gates
  auth.py        OIDC token -> Principal (provider-neutral)
  authz.py       access rules callable from Python, delegating to the SQL functions
  audit.py       the Auditor: one way to record allowed and refused actions
  schema.py      ApiModel, the base for every request and response model
  paging.py      keyset page tokens, newest first, for every list the API pages
  ports.py       interfaces to the outside world (clock, ids, blobs, queues)
  adapters/      real implementations of ports (Azure Blob and SAS signing, Service Bus,
                 the EICAR stand-in scanner, the process sandbox format readers and
                 derive recipes run in)
  wiring.py      builds adapters from settings, for the API, the worker and the shim
  events.py      BlobCreated events in the Event Grid schema: parsed by workers, built by the shim
  event_shim.py  local stand-in for Event Grid: announces committed landing blobs on the queue
  meta.py        /healthz, /config
  identity.py    /me
  collections/   collections and their grants: models.py, store.py, routes.py
  uploads/       upload sessions and their signed URLs: models.py, store.py, routes.py; and
                 their live status (events.py: the bounded stream behind /uploads/events)
  items/         cataloged files: kinds.py (what a file is), metadata.py (what its header
                 says), models.py (STAC Items and searches), store.py (only the pipeline
                 writes; reads and keyset search), routes.py (/items/{id}, /search,
                 /items/{id}/assets/{role}: an audited read URL for one file, and
                 /items/{id}/preview: an audited preview token for the tiler)
  tiles/         map tiles: footprint views (store.py: the audited sessions a map's tiles
                 are served under) and their vector tiles, drawn by the items store's own
                 search query (routes.py: /tiles/footprints); TileJSON shapes (models.py)
  previews.py    preview tokens: minted by the API for one readable item's COG, checked by
                 the tiler
  display.py     how a raster is drawn as a picture, by its thumbnail and its preview tiles
  tiler/         its own service, with no database and no login: one item's raster tiles under
                 a preview token (app.py: composition; routes.py: /tiles/items/...; raster.py:
                 the COG read through the blob port within a per-request budget, and drawn)
  worker/        the pipeline: pipeline.py (one blob version's step machine), classify.py,
                 extract.py (budgeted header reads), formats/ (one reader per format family),
                 outbox.py (derive requests, written with the outcome and relayed to the
                 derive queue); the derive stage: derive.py (one item's derivatives),
                 recipe.py (what a recipe is), recipes/ (one per kind and role); and for both:
                 jobs.py (their store), runner.py (queue -> settle, dead-letter sweep),
                 selfcheck.py (`--check`), __main__.py (composition; `--derive` for the stage)
api/typings/     local type stubs for the parts of untyped libraries we use (rasterio, pyarrow)
api/migrations/  numbered SQL, never edited once merged; add a new file instead
```

- **Packages are named for what they own.** The *catalog* is collections plus items (the
  `catalog` schema); each has its own package because each has its own writer.
- **A feature is a module; it becomes a package once it owns storage.** A package has
  `models.py` (Pydantic request and response shapes), `store.py` (SQL, typed rows in and out)
  and `routes.py` (HTTP only: parse, call, audit, map errors). Add a feature by adding a
  package and one `include_router` line in `app.py`; nothing else should need to change.
- **The API schema is the contract with the web app.** Web types are generated from it
  (`make api-types`, checked in CI); never hand-write a type for an API response.
- **Routes hold no SQL and stores hold no HTTP.** A store function takes a `Connection` and
  typed arguments and returns typed rows or raises a domain error; routes turn those into
  status codes.
- **The database owns authorization.** Who may read what is decided in one SQL function
  (`authz.readable_collections`) that every query filters through. Python asks the database;
  it never re-implements the rule.
- **Side effects go through ports.** Production code never calls `time`, `uuid`, `random` or
  a cloud SDK directly outside `adapters/`; it takes a port, so the simulator can substitute a
  seeded fake.
- **Composition happens once per process,** at startup: `app.py` for the API, `tiler/app.py`
  for the tiler, `worker/__main__.py` and `event_shim.run` for the daemons, and test fixtures.
  Each builds its adapters with `wiring.py`, never by hand. No module-level clients, pools or
  singletons.

## Code standards

### Duplicated logic is a defect, even when it works

Two copies of a rule will drift, and the copy nobody remembers is the one that ships the
bug. Treat duplication as a failing review item, not a style preference.

- One source of truth for every rule, query fragment, constant, validation pattern and
  mapping. If you are about to write something that already exists, import it; if it is not
  importable, refactor until it is.
- That includes data: test users are read from the Keycloak realm file, marking codes from
  the database, and the seed path from `migrate.py`, not retyped.
- Similar-looking code with different reasons to change is not duplication. Don't merge two
  things just because they currently share three lines.
- **The one deliberate exception** is `api/tests/model.py`, an independent reference
  implementation of authorization that tests compare against. Its value is that it was
  written separately; never import production code into it, or production rules from it.

### Power of 10, adapted for Python

NASA/JPL's rules for safety-critical code, translated to this codebase. They are mandatory in
critical code (authorization, audit, pipeline state transitions, migrations, anything that
touches markings) and the default everywhere else.

1. **Simple control flow.** Early returns over nesting; no recursion over untrusted input; no
   `try` blocks used for flow control. If a function needs a diagram, split it.
2. **Every loop has a fixed upper bound.** Retries, pagination, polling, range reads and queue
   receives all have an explicit, named limit. `while True` needs a bound and a comment.
3. **Bound all memory in request and worker paths.** Never read a whole blob or result set
   into memory without a size cap; range-read and stream. Every string and list a client can
   send has a `max_length`.
4. **Short functions with one job.** Around 40 statements at most; ruff enforces complexity
   (`C901` at 10, `PLR` limits on arguments, branches and returns).
5. **Check assumptions explicitly.** Validate inputs at the edge (Pydantic, SQL `CHECK`
   constraints) and state preconditions in critical code with explicit checks that raise
   typed errors. Never use `assert` in production code: `python -O` strips it.
6. **Smallest possible scope.** No module-level mutable state; frozen dataclasses for values;
   dependencies passed in, never fetched from globals.
7. **Handle every error and every return value.** No bare `except`, no swallowed exceptions;
   catch the narrowest exception, and translate it at the boundary (`adapters/`, `store.py`)
   into a domain error. A refused request is audited before it is raised.
8. **No metaprogramming.** No monkeypatching, `exec`, `eval`, dynamic attributes or custom
   metaclasses in application code. Decorators only from the frameworks we use.
9. **Limit indirection.** Composition over inheritance; `Protocol` over base classes; no
   inheritance deeper than one level outside framework types (Pydantic, exceptions).
10. **Zero warnings from day one.** `ruff` and `pyright` (strict) are clean on every commit.
    A suppression (`# noqa`, `# pyright: ignore`) names the rule and says why on the same line.

### Type-safe, idiomatic Python

- **pyright strict passes** for `ingest_api/` and `tools/`; tests run in strict mode with
  fixture-inference rules relaxed (see `pyproject.toml`).
- **Annotate every function signature,** including return types. `Any` appears only where
  untyped data enters (JWT claims, JSON from outside) and is narrowed immediately.
- **Untyped libraries get stubs, not ignores.** Declare what production code uses in
  `api/typings/`; a stub's module-level `__getattr__` leaves the rest as `Any` for tests.
- **Model the domain in types, not strings.** `StrEnum` for closed sets the code branches on
  (`Role`, `AuditAction`), `Literal` for small fixed sets in API shapes, frozen
  `@dataclass(slots=True)` for internal values, Pydantic models only at the HTTP edge.
- **Typed database rows.** Use `psycopg.rows.class_row` with a dataclass, never `dict_row`
  results passed around as `dict[str, Any]`.
- **Modern syntax.** `X | None`, built-in generics (`list[str]`), `collections.abc` for
  abstract types, `pathlib` for paths, f-strings, `match` where it reads better than `elif`
  chains.
- **Keyword-only arguments** (`*`) for anything past the first two parameters, and for every
  boolean flag (ruff `FBT`).
- **Context managers own resources:** connections, transactions, clients, files.
- **Names say what, comments say why.** Docstrings on public modules, classes and anything
  non-obvious; no comments that restate the code.

## Reading failures

`make check` writes `api/reports/invariants.json`:

```json
{
  "ok": false,
  "invariants": {"ACC-1": {"status": "enforced", "result": "failed", "failed_tests": ["..."]}},
  "failures": [
    {"test": "tests/sim/...", "invariants": ["ACC-1"], "error": "[ACC-1] ...",
     "repro": "uv run pytest '...'  # or add @reproduce_failure(...)", "detail": "..."}
  ]
}
```

Start from `failures[].invariants` and `error`. Simulator failures include the shrunk steps in
`detail`; replay them with the `repro` command. Hypothesis also keeps failing examples in
`api/.hypothesis/` and retries them first on the next run.

## How the harness is built

- `api/tests/model.py`: the independent reference model of who may read what.
- `api/tests/sim/`: Hypothesis state machines that drive the real API and Postgres with
  random actors and operations, checking invariants after every step.
  `test_catalog_sim.py` covers catalog changes, upload sessions, downloads and preview tokens
  (ACC-*);
  `test_pipeline_sim.py` runs the worker over a faulty queue and blob store, then drains and
  replays every event (ING-1 to ING-5); `test_derive_sim.py` relays derive requests to a
  faulty queue and runs the derive stage over a faulty store, then replays every request and
  rebuilds every derivative, shipping a new recipe version midway (ING-7, ING-11 to ING-13); `test_search_sim.py` pages through random
  searches while items arrive, against its own model of the filters, and draws random footprint
  views (SRCH-1, SRCH-2, ACC-9).
- `api/tests/strategies.py`: the shared Hypothesis strategies (actors, grantees, names).
- `api/tests/items.py`: catalogs items straight through the pipeline's stores, for read and
  search tests that don't need bytes (or, given `content`, derive tests that do).
- `api/tests/recipes.py`: small recipes, each with one trait the derive stage must handle;
  `test_raster_recipes.py` runs the real ones, and `test_sandbox.py` misbehaving ones.
- `api/tests/fakes.py`: seeded, fault-injecting implementations of the ports, and
  `CrashAfter`, which interrupts a job after its N-th storage call (`test_pipeline.py`).
- `api/tests/contracts/`: one behavioural suite per port, run against the fake *and* the real
  thing (Azurite, the Service Bus emulator), so fakes can't drift from reality.
  `test_signed_urls.py` also proves Azurite enforces the limits a signed URL claims, for
  upload and read URLs alike (ACC-8).
- `api/tests/test_fuzz.py`, `test_robustness.py`: schema-driven and targeted fuzzing (ROB-1,
  ACC-1).
- `api/tests/e2e/` (`make e2e`, deselected from `make check`): the sample corpus through the
  running stack. Expectations live only in `samples/manifest.json`, taken from sources
  independent of our readers; a sample that fails there is a finding, not a manifest edit.

## Conventions

- Python 3.12, FastAPI, psycopg 3, `uv`. `ruff` for lint and format, line length 100.
- Web: React + Vite + TypeScript (strict), `pnpm`, `oxlint`, `vitest` + `fast-check`.
- Markings, roles and clearances are data (`authz.markings`, OIDC claims), not code.
- Every route that touches catalog data writes exactly one audit event and commits it before
  responding (ACC-5); use the `Auditor`, never insert audit rows directly.
- Commits: subject at most 72 characters, body wrapped at 100.
