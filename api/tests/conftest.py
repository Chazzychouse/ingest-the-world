import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from ingest_api.app import create_app
from ingest_api.auth import OidcVerifier, StaticJwks
from ingest_api.migrate import migrate
from ingest_api.settings import Settings
from tests.tokens import AUDIENCE, ISSUER, TestIdp

ADMIN_URL = os.environ.get(
    "INGEST_TEST_DATABASE_URL", "postgresql://ingest:ingest@localhost:5432/ingest"
)


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


@pytest.fixture(scope="session")
def database_url():
    """A fresh, migrated and seeded database per test session, dropped afterwards."""
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("INGEST_REQUIRE_DB"):
            raise
        pytest.skip(f"Postgres not reachable at {ADMIN_URL} ({exc}); run `docker compose up db`")
    name = f"ingest_test_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    url = _with_database(ADMIN_URL, name)
    try:
        migrate(url, seed=True)
        yield url
    finally:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture(scope="session")
def idp() -> TestIdp:
    return TestIdp()


@pytest.fixture(scope="session")
def client(database_url, idp):
    settings = Settings(database_url=database_url, oidc_issuer=ISSUER, oidc_audience=AUDIENCE)
    verifier = OidcVerifier(
        issuer=ISSUER, audience=AUDIENCE, keys=StaticJwks(idp.jwks), algorithms=["RS256"]
    )
    with TestClient(create_app(settings, verifier)) as test_client:
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
