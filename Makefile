.PHONY: up down reset logs services test check explore sim soak mutants lint typecheck api-types api-types-check tf-validate token samples e2e image-smoke

# ACCEPT_EULA=Y in .env accepts the Service Bus emulator's and SQL Server's licences, which
# adds the queue to the stack and to the tests (see .env.example). Without it both skip.
-include .env
ifeq ($(ACCEPT_EULA),Y)
export COMPOSE_PROFILES = queue
QUEUE = servicebus
export INGEST_REQUIRE_SERVICEBUS = 1
endif

# Waits for the emulator's health endpoint, which compose can't check (the image has no shell).
define wait_for_queue
	$(if $(QUEUE),@for i in $$(seq 60); do \
	  curl -sf http://localhost:5300/health >/dev/null && exit 0; sleep 2; done; \
	  echo "Service Bus emulator not healthy after 120s: docker compose logs servicebus"; exit 1)
endef

up:  ## Build and start the local stack (http://localhost:8081)
	docker compose up -d --build --wait
	$(wait_for_queue)

down:
	docker compose down

reset:  ## Stop and delete local data volumes
	docker compose down -v

logs:
	docker compose logs -f api $(if $(QUEUE),event-shim worker)

services:  # the backing services the tests run against
	docker compose up -d --wait db azurite $(QUEUE)
	$(wait_for_queue)

PYTEST = cd api && INGEST_REQUIRE_DB=1 uv run pytest -q

test: services  ## API tests against the Compose Postgres, Azurite and Service Bus emulator
	$(PYTEST)

# Hypothesis profile per target (see api/tests/conftest.py). `check` is derandomized so a
# local pass predicts CI; `explore` and `soak` look for new failures.
check: export INGEST_HYPOTHESIS_PROFILE = ci
explore sim: export INGEST_HYPOTHESIS_PROFILE = dev
soak: export INGEST_HYPOTHESIS_PROFILE = soak

check: lint typecheck api-types-check services  ## The gate: lint, types, every test, traceability, JSON report
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

mutants: services  ## Plant known bugs and check each is caught by the right invariant
	cd api && INGEST_REQUIRE_DB=1 uv run python tools/mutants.py

LARGE ?=
samples:  ## Fetch, make and verify the sample corpus (samples/manifest.json): LARGE=1 for multi-GB
	cd api && PYTHONPATH=. uv run python tools/samples.py $(if $(LARGE),--large)

e2e: samples  ## Upload the corpus through the running stack (make up) and check every outcome
	cd api && $(if $(LARGE),INGEST_E2E_LARGE=1) uv run pytest -q -m e2e tests/e2e

IMAGE ?= ingest-api:smoke
image-smoke:  ## Build the API image and check its native readers load and run (as CI does)
	docker build -t $(IMAGE) api
	docker run --rm $(IMAGE) python -m ingest_api.worker --check
	docker run --rm $(IMAGE) python -c "import ingest_api.app, ingest_api.event_shim"

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
