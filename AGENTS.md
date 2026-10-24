# Working in this repo

Guidance for AI agents and humans alike. `CLAUDE.md` imports this file.

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

All from the repo root. Postgres and Azurite come up automatically.

| Command | Use it when |
|---|---|
| `make check` | Before saying any work is done. Lint, all tests (derandomized, same as CI), invariant traceability, and `api/reports/invariants.json`. |
| `make explore` | After touching authorization, audit or the pipeline: property tests and the simulator with fresh random inputs. |
| `make sim SEED=n` | Replaying a simulator failure from its seed. |
| `make soak` | Long simulator run before a milestone. |
| `make mutants` | After changing tests or checks: plants known bugs and verifies each is caught by the right invariant. |
| `make up` | Running the full local stack (http://localhost:8081). |

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

- `api/tests/model.py`: an independent reference model of who may read what. Tests compare
  the SQL predicate and the API against it.
- `api/tests/sim/`: Hypothesis state machines that drive the real API and Postgres with
  random actors and operations, checking invariants after every step. Pipeline actors
  (uploads, workers, faulty queues) join in M1.
- `api/ingest_api/ports.py`: clock, ids, blob store and queue interfaces. Production code
  depends on these, never on `time`, `uuid` or an SDK directly, so the simulator can swap in
  `api/tests/fakes.py` (seeded and fault-injecting).
- `api/tests/contracts/`: one behavioural suite per port, run against the fake *and* the real
  thing (Azurite today; the Service Bus emulator in M1), so fakes can't drift from reality.
- `api/tests/test_fuzz.py`: schema-driven fuzzing of every route as several users (ROB-1,
  ACC-1).

## Conventions

- Python 3.12, FastAPI, psycopg 3, `uv`. `ruff` for lint and format, line length 100.
- Web: React + Vite + TypeScript, `pnpm`, `oxlint`.
- Markings, roles and clearances are data (`authz.markings`, OIDC claims), not code.
- Every route that touches catalog data writes exactly one audit event and commits it before
  responding (ACC-5).
