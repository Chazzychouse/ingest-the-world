.PHONY: up down reset logs test lint tf-validate token

up:  ## Build and start the local stack (http://localhost:8081)
	docker compose up -d --build --wait

down:
	docker compose down

reset:  ## Stop and delete local data volumes
	docker compose down -v

logs:
	docker compose logs -f api

test:  ## API tests against the Compose Postgres
	docker compose up -d --wait db
	cd api && INGEST_REQUIRE_DB=1 uv run pytest -q

lint:
	cd api && uv run ruff check . && uv run ruff format --check .
	cd web && pnpm lint

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
