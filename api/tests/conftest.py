import json
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, Phase, settings
from psycopg import sql
from pydantic import SecretStr

from ingest_api.app import create_app
from ingest_api.migrate import DEV_SEED, migrate, sql_file
from ingest_api.settings import Settings
from tests.tokens import AUDIENCE, ISSUER, TestIdp

pytest_plugins = ["tests.invariants"]

# Hypothesis profiles: `dev` explores new random inputs each run, `ci` is derandomized so a
# red build reproduces exactly, `soak` runs long, `mutants` only needs to detect a failure.
# Pick with INGEST_HYPOTHESIS_PROFILE.
settings.register_profile(
    "base",
    deadline=None,  # every example touches a real database
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    print_blob=True,
)
_base = settings.get_profile("base")
settings.register_profile("dev", _base, max_examples=25, stateful_step_count=30)
settings.register_profile("ci", _base, max_examples=60, stateful_step_count=40, derandomize=True)
settings.register_profile(
    "mutants",
    settings.get_profile("ci"),
    print_blob=False,
    database=None,
    phases=[Phase.explicit, Phase.generate],
)
settings.register_profile("soak", _base, max_examples=2000, stateful_step_count=100)
settings.load_profile(os.environ.get("INGEST_HYPOTHESIS_PROFILE", "dev"))

ADMIN_URL = os.environ.get(
    "INGEST_TEST_DATABASE_URL", "postgresql://ingest:ingest@localhost:5432/ingest"
)
REALM = Path(__file__).resolve().parents[2] / "infra" / "keycloak" / "ingest-realm.json"
# Tests use their own Azurite account, so the local stack's worker never touches their blobs
# (and theirs never reach its queue). Compose registers it alongside the default account; the
# key is a test-only value, checked against compose.yaml by tests/test_infra_parity.py.
TEST_ACCOUNT = "ingesttest"
TEST_ACCOUNT_KEY = "aW5nZXN0LXRoZS13b3JsZC10ZXN0LWFjY291bnQta2V5LTMyYg=="
TEST_ACCOUNT_URL = f"http://127.0.0.1:10000/{TEST_ACCOUNT}"
# The Service Bus emulator's fixed connection string, as seen from the host:
# https://github.com/Azure/azure-service-bus-emulator-installer#choosing-the-right-connection-string
EMULATOR_CONNECTION = (
    "Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;"
    "SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;"
)


def _realm_users() -> dict[str, list[str]]:
    """Demo users and their realm roles, read from the Keycloak realm the stack imports."""
    realm = json.loads(REALM.read_text())
    default_role = f"default-roles-{realm['realm']}"  # Keycloak adds it to every user
    return {u["username"]: [*u.get("realmRoles", []), default_role] for u in realm["users"]}


USERS = {
    **_realm_users(),
    # Test-only principals the demo realm deliberately doesn't have.
    "admin-no-cui": ["Admin"],
    "nobody": ["default-roles-ingest"],
}


def _with_database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def _admin_connection() -> psycopg.Connection:
    try:
        return psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("INGEST_REQUIRE_DB"):
            raise
        pytest.skip(f"Postgres not reachable at {ADMIN_URL} ({exc}); run `docker compose up db`")


@pytest.fixture(scope="session")
def fresh_database() -> Iterator[Callable[[], str]]:
    """Factory for isolated databases cloned from one migrated, seeded template.

    Every database made here is dropped at the end of the session.
    """
    admin = _admin_connection()
    run = uuid.uuid4().hex[:8]
    template = f"ingest_tpl_{run}"
    created = [template]
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(template)))
    migrate(_with_database(ADMIN_URL, template), seed=True)

    def clone() -> str:
        name = f"ingest_test_{run}_{len(created)}"
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template)
            )
        )
        created.append(name)
        return _with_database(ADMIN_URL, name)

    try:
        yield clone
    finally:
        for name in reversed(created):
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
        admin.close()


def reset_catalog(conn: psycopg.Connection) -> None:
    """Puts collections and grants back to the seed, with no uploads, jobs or items. Audit
    events stay: they're append-only, and checks read them as a delta from a high-water mark."""
    with conn.transaction():
        conn.execute("DELETE FROM ingest.jobs")
        conn.execute("DELETE FROM catalog.items")
        conn.execute("DELETE FROM ingest.uploads")
        conn.execute("DELETE FROM authz.collection_grants")
        conn.execute("DELETE FROM catalog.collections")
        conn.execute(sql_file(DEV_SEED))


@pytest.fixture(scope="session")
def database_url(fresh_database: Callable[[], str]) -> str:
    """The database shared by the example-based API tests."""
    return fresh_database()


@pytest.fixture(scope="session")
def idp() -> TestIdp:
    return TestIdp()


def app_settings(database_url: str) -> Settings:
    """Settings for an app under test: the test IdP, the tests' Azurite account for signing
    upload URLs, and the Service Bus emulator with short receive waits."""
    return Settings(
        database_url=database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        blob_account_url=TEST_ACCOUNT_URL,
        blob_account_key=SecretStr(TEST_ACCOUNT_KEY),
        queue_connection=SecretStr(EMULATOR_CONNECTION),
        queue_max_wait_seconds=1.0,
    )


def make_app(database_url: str, idp: TestIdp) -> FastAPI:
    return create_app(app_settings(database_url), idp.verifier())


@pytest.fixture(scope="session")
def client(database_url: str, idp: TestIdp) -> Iterator[TestClient]:
    with TestClient(make_app(database_url, idp)) as test_client:
        yield test_client


@pytest.fixture
def db(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def auth(idp: TestIdp) -> Callable[[str], dict[str, str]]:
    """Headers for a named user from `USERS`; their subject is `sub-<name>`."""

    def headers(user: str) -> dict[str, str]:
        return idp.bearer(f"sub-{user}", roles=USERS[user])

    return headers
