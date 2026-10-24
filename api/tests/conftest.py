import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, Phase, settings

from ingest_api.app import create_app
from ingest_api.auth import OidcVerifier, StaticJwks
from ingest_api.migrate import migrate
from ingest_api.settings import Settings
from tests.tokens import AUDIENCE, ISSUER, TestIdp

pytest_plugins = ["tests.invariants"]

# Hypothesis profiles: `dev` explores new random inputs each run, `ci` is derandomized so a
# red build reproduces exactly, `soak` runs long. Pick with INGEST_HYPOTHESIS_PROFILE.
_common = {
    "deadline": None,  # every example touches a real database
    "suppress_health_check": [HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    "print_blob": True,
}
settings.register_profile("dev", max_examples=25, stateful_step_count=30, **_common)
settings.register_profile(
    "ci", max_examples=60, stateful_step_count=40, derandomize=True, **_common
)
# For tools/mutants.py: only detection matters, so skip shrinking and the example database.
settings.register_profile(
    "mutants",
    **{**_common, "print_blob": False},
    max_examples=60,
    stateful_step_count=40,
    derandomize=True,
    database=None,
    phases=[Phase.explicit, Phase.generate],
)
settings.register_profile("soak", max_examples=2000, stateful_step_count=100, **_common)
settings.load_profile(os.environ.get("INGEST_HYPOTHESIS_PROFILE", "dev"))

ADMIN_URL = os.environ.get(
    "INGEST_TEST_DATABASE_URL", "postgresql://ingest:ingest@localhost:5432/ingest"
)


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def _admin_connection() -> psycopg.Connection:
    try:
        return psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("INGEST_REQUIRE_DB"):
            raise
        pytest.skip(f"Postgres not reachable at {ADMIN_URL} ({exc}); run `docker compose up db`")


@pytest.fixture(scope="session")
def fresh_database():
    """Factory for isolated databases cloned from one migrated, seeded template.

    Cloning a template takes a fraction of a second, so the simulator can afford a clean
    database per example. Every database made here is dropped at the end of the session.
    """
    admin = _admin_connection()
    run = uuid.uuid4().hex[:8]
    template = f"ingest_tpl_{run}"
    created = [template]
    admin.execute(f'CREATE DATABASE "{template}"')
    migrate(_with_database(ADMIN_URL, template), seed=True)

    def clone() -> str:
        name = f"ingest_test_{run}_{len(created)}"
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
        created.append(name)
        return _with_database(ADMIN_URL, name)

    try:
        yield clone
    finally:
        for name in reversed(created):
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


SEED = Path(__file__).resolve().parents[1] / "seed" / "dev.sql"


def reset_catalog(conn: psycopg.Connection) -> None:
    """Puts collections and grants back to the seed. Audit events stay: they're append-only,
    and checks read them as a delta from a high-water mark."""
    with conn.transaction():
        conn.execute("DELETE FROM authz.collection_grants")
        conn.execute("DELETE FROM catalog.collections")
        conn.execute(SEED.read_text())


@pytest.fixture(scope="session")
def database_url(fresh_database):
    """The database shared by the example-based API tests."""
    return fresh_database()


@pytest.fixture(scope="session")
def idp() -> TestIdp:
    return TestIdp()


def make_app(database_url: str, idp: TestIdp):
    settings = Settings(database_url=database_url, oidc_issuer=ISSUER, oidc_audience=AUDIENCE)
    verifier = OidcVerifier(
        issuer=ISSUER, audience=AUDIENCE, keys=StaticJwks(idp.jwks), algorithms=["RS256"]
    )
    return create_app(settings, verifier)


@pytest.fixture(scope="session")
def client(database_url, idp):
    with TestClient(make_app(database_url, idp)) as test_client:
        yield test_client


@pytest.fixture
def db(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        yield conn


USERS = {
    # Mirrors infra/keycloak/ingest-realm.json, including Keycloak's default realm role.
    "alice": ["Admin", "Clearance.CUI", "default-roles-ingest"],
    "carol": ["Contributor", "Clearance.CUI", "default-roles-ingest"],
    "bob": ["Reader", "default-roles-ingest"],
    "admin-no-cui": ["Admin"],
    "nobody": ["default-roles-ingest"],
}


@pytest.fixture
def auth(idp):
    def headers(user: str) -> dict[str, str]:
        token = idp.token(sub=f"sub-{user}", preferred_username=user, roles=USERS[user])
        return {"Authorization": f"Bearer {token}"}

    return headers
