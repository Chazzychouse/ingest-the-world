from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ingest_api.auth import DEFAULT_CLAIMS


class Settings(BaseSettings):
    """Runtime configuration. Every value comes from `INGEST_*` environment variables.

    The OIDC block is provider-neutral: the same build runs against Keycloak locally,
    commercial Entra ID (`login.microsoftonline.com`) or Azure Government Entra ID
    (`login.microsoftonline.us`) by changing configuration only.
    """

    model_config = SettingsConfigDict(env_prefix="INGEST_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://ingest:ingest@localhost:5432/ingest"

    # Expected `iss` claim, and the base for OIDC discovery.
    oidc_issuer: str = "http://localhost:8080/realms/ingest"
    # Expected `aud` claim on access tokens (Entra: the API's client id or app id URI).
    oidc_audience: str = "ingest-api"
    # Overrides the discovered JWKS URI. Needed when the API reaches the IdP on a
    # different host than the browser does (e.g. `keycloak:8080` inside Compose).
    oidc_jwks_uri: str | None = None
    oidc_algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    # Claim names; dotted paths reach into nested objects (e.g. `realm_access.roles`).
    oidc_roles_claim: str = DEFAULT_CLAIMS.roles
    oidc_groups_claim: str = DEFAULT_CLAIMS.groups
    oidc_clearance_claim: str = DEFAULT_CLAIMS.clearance

    # Public values handed to the SPA by `GET /config`, so one web build serves every cloud.
    web_oidc_authority: str = "http://localhost:8080/realms/ingest"
    web_oidc_client_id: str = "ingest-web"
    web_oidc_scope: str = "openid profile email"

    cors_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
