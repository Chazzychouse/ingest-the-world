from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ingest_api.auth import DEFAULT_CLAIMS

# The longest a signed URL may live (ACC-8). Large uploads renew theirs instead.
MAX_SIGNED_URL_MINUTES = 60


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

    # Blob Storage. Locally the API signs upload URLs with Azurite's account key. In the cloud the
    # key is left unset and it signs with a user-delegation key from its managed identity, since
    # shared-key access is disabled on the account.
    blob_account_url: str = "http://127.0.0.1:10000/devstoreaccount1"
    blob_account_key: SecretStr | None = None
    # Where browsers reach Blob Storage, when that differs from `blob_account_url` (Compose).
    blob_public_url: str | None = None
    upload_url_minutes: int = Field(default=MAX_SIGNED_URL_MINUTES, gt=0, le=MAX_SIGNED_URL_MINUTES)

    # Service Bus. Locally a connection string for the emulator; in the cloud the namespace's
    # host name (`sb-x.servicebus.usgovcloudapi.net`) and the process's managed identity, since
    # local (SAS) auth is disabled on the namespace.
    queue_connection: SecretStr | None = None
    queue_namespace: str | None = None
    queue_name: str = "ingest"
    # How long a receive waits for a message before returning none.
    queue_max_wait_seconds: float = Field(default=5.0, gt=0, le=60)

    # Messages a worker takes per receive. It handles them one at a time.
    worker_batch: int = Field(default=4, gt=0, le=32)

    # The local event shim's polling interval (`ingest_api.event_shim`).
    shim_poll_seconds: float = Field(default=1.0, gt=0, le=60)

    cors_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
