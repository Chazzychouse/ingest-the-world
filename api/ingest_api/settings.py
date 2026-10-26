from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ingest_api.auth import DEFAULT_CLAIMS
from ingest_api.previews import MIN_KEY_LENGTH

# The longest a signed URL may live (ACC-8). Large uploads renew theirs instead; a download
# starts at once, and only needs its URL to outlive the start.
MAX_SIGNED_URL_MINUTES = 60
MAX_READ_URL_MINUTES = 15
# The longest a map session (a footprint view, an item preview) serves tiles unaudited (ACC-5).
MAX_MAP_SESSION_MINUTES = 15


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
    # Origins that may send blocks straight to Blob Storage, applied to the account at startup.
    # Only for emulators, which start with no CORS rules; in the cloud Terraform owns the rule
    # (`infra/terraform/storage.tf`), so this stays empty there.
    blob_cors_origins: list[str] = Field(default_factory=list, max_length=16)
    upload_url_minutes: int = Field(default=MAX_SIGNED_URL_MINUTES, gt=0, le=MAX_SIGNED_URL_MINUTES)
    read_url_minutes: int = Field(default=MAX_READ_URL_MINUTES, gt=0, le=MAX_READ_URL_MINUTES)
    map_session_minutes: int = Field(
        default=MAX_MAP_SESSION_MINUTES, gt=0, le=MAX_MAP_SESSION_MINUTES
    )

    # The key preview tokens are signed with: the API mints them, the tiler checks them, and
    # nothing else holds it. Without one the API offers no previews and the tiler won't start.
    preview_key: SecretStr | None = Field(default=None, min_length=MIN_KEY_LENGTH)
    # Live upload status (`GET /uploads/events`): how often a stream asks the database, and how
    # long it lasts before the client reopens it.
    events_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    events_stream_seconds: float = Field(default=300.0, gt=0, le=3600)
    # Where browsers reach the tiler, which serves `/tiles/items/...`: empty for the web app's own
    # origin, where nginx routes `/tiles/` to it.
    tiler_url: str = ""

    # Service Bus. Locally a connection string for the emulator; in the cloud the namespace's
    # host name (`sb-x.servicebus.usgovcloudapi.net`) and the process's managed identity, since
    # local (SAS) auth is disabled on the namespace.
    queue_connection: SecretStr | None = None
    queue_namespace: str | None = None
    queue_name: str = "ingest"
    # How long a receive waits for a message before returning none.
    queue_max_wait_seconds: float = Field(default=5.0, gt=0, le=60)
    # Deliveries before the queue dead-letters a message; the queue's own setting
    # (`infra/terraform/messaging.tf`), so the worker knows a last delivery when it sees one.
    queue_max_deliveries: int = Field(default=5, gt=0, le=100)
    # How long a held message's lock is renewed while its job runs (ING-10). Covers copying and
    # hashing the largest file the platform takes; 0 turns renewal off.
    queue_lock_renewal_seconds: float = Field(default=3600.0, ge=0, le=86400)

    # The queue derive requests are relayed to from the outbox, and that the derive stage takes
    # them from (`python -m ingest_api.worker --derive`).
    derive_queue_name: str = "derive"
    # Where the derive stage renders; each render gets, and then removes, a directory under it.
    derive_scratch_dir: Path = Path("/tmp/ingest-derive")  # noqa: S108 - a container-local default

    # Messages a worker takes per receive. It handles them one at a time.
    worker_batch: int = Field(default=4, gt=0, le=32)
    # How often the worker sweeps the dead-letter queue, giving up each version found there.
    dead_letter_sweep_seconds: float = Field(default=60.0, gt=0, le=3600)

    # Limits on the child process each extraction's reader runs in (ING-8).
    extract_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    extract_memory_mib: int = Field(default=1024, ge=256, le=16384)
    # And on the one each derivative is rendered in: a COG of a 2 GiB original takes minutes.
    derive_timeout_seconds: float = Field(default=1800.0, gt=0, le=7200)
    derive_memory_mib: int = Field(default=2048, ge=512, le=16384)

    # The local event shim's polling interval (`ingest_api.event_shim`).
    shim_poll_seconds: float = Field(default=1.0, gt=0, le=60)

    cors_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
