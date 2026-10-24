.PHONY: up down reset logs test check explore sim soak mutants lint typecheck api-types api-types-check tf-validate token

up:  ## Build and start the local stack (http://localhost:8081)
	docker compose up -d --build --wait

down:
	docker compose down

reset:  ## Stop and delete local data volumes
	docker compose down -v

logs:
	docker compose logs -f api

PYTEST = cd api && INGEST_REQUIRE_DB=1 uv run pytest -q

test:  ## API tests against the Compose Postgres (and Azurite)
	docker compose up -d --wait db azurite
	$(PYTEST)

# Hypothesis profile per target (see api/tests/conftest.py). `check` is derandomized so a
# local pass predicts CI; `explore` and `soak` look for new failures.
check: export INGEST_HYPOTHESIS_PROFILE = ci
explore sim: export INGEST_HYPOTHESIS_PROFILE = dev
soak: export INGEST_HYPOTHESIS_PROFILE = soak

check: lint typecheck api-types-check  ## The gate: lint, types, every test, traceability, JSON report
	docker compose up -d --wait db azurite
	$(PYTEST) --check-invariants --invariants-report=reports/invariants.json
	cd web && pnpm test

explore:  ## Property tests and simulation with fresh random inputs
	docker compose up -d --wait db
	$(PYTEST) tests/sim tests/test_access_model.py

SEED ?= 0
sim:  ## Replay the simulation from a seed: make sim SEED=123
	docker compose up -d --wait db
	$(PYTEST) tests/sim --hypothesis-seed=$(SEED)

soak:  ## Long simulation run (minutes)
	docker compose up -d --wait db
	$(PYTEST) tests/sim tests/test_access_model.py

mutants:  ## Plant known bugs and check each is caught by the right invariant
	docker compose up -d --wait db azurite
	cd api && INGEST_REQUIRE_DB=1 uv run python tools/mutants.py

lint:
	cd api && uv run ruff check . && uv run ruff format --check .
	cd web && pnpm lint

typecheck:  ## pyright strict (API) and tsc strict (web)
	cd api && uv run pyright
	cd web && pnpm exec tsc -b

OPENAPI = web/src/generated/openapi.json
api-types:  ## Regenerate the web's API types from the API's OpenAPI schema
	cd api && PYTHONPATH=. uv run python tools/export_openapi.py ../$(OPENAPI)
	cd web && pnpm gen:api

api-types-check: api-types  ## Fail if the committed web types are stale
	git diff --exit-code -- web/src/generated || \
	  (echo "web/src/generated is stale: run make api-types and commit"; exit 1)

tf-validate:  ## fmt-check and validate the Terraform for both clouds
	infra/tf.sh fmt -check -recursive
	infra/tf.sh init -backend=false -input=false >/dev/null
	infra/tf.sh validate

AS ?= alice
token:  ## Print a dev access token: make token AS=carol
	@curl -s -d grant_type=password -d client_id=ingest-web -d username=$(AS) \
	  -d password=$(AS) -d scope=openid \
	  http://localhost:8080/realms/ingest/protocol/openid-connect/token \
	  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
