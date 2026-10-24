"""`BlobStore` and `BlobSigner` over Azure Blob Storage (and Azurite)."""

import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from typing import NoReturn
from urllib.parse import quote, urlsplit

from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobProperties,
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    UserDelegationKey,
    generate_blob_sas,
)

from ingest_api.ports import (
    BlobChanged,
    BlobExists,
    BlobNotFound,
    BlobProps,
    Clock,
    StorageError,
    TransientStorageError,
)

# Throttling and server-side failures; anything else is a caller or configuration error.
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RANGE_NOT_SATISFIABLE = 416
PRECONDITION_FAILED = 412
# Copies within one account usually finish at once; a large one is polled this many times.
MAX_COPY_POLLS = 60
COPY_POLL_SECONDS = 1.0

# Put Blob, Put Block and Put Block List need create and write; nothing else is granted (ACC-8).
UPLOAD_PERMISSION = BlobSasPermissions(create=True, write=True)
# User-delegation keys are fetched with this lifetime and reused until they no longer outlive
# the URL being signed, so signing costs one key request per few hours, not one per upload.
DELEGATION_KEY_LIFETIME = timedelta(hours=6)
# Backdates key validity so a storage clock slightly behind ours still accepts new URLs.
CLOCK_SKEW = timedelta(minutes=5)


def account_name(account_url: str) -> str:
    """The storage account an endpoint belongs to.

    Cloud endpoints name it in the host (`https://acct.blob.core.usgovcloudapi.net`); emulators
    use path-style URLs and name it in the first path segment (`http://azurite:10000/acct`).
    The SDK only recognises path-style URLs on IP addresses and localhost; for a named host
    (Compose's `azurite`) it finds no name, and every URL signed without one is refused.
    """
    parts = urlsplit(account_url)
    if not parts.hostname:
        raise ValueError(f"not a storage account URL: {account_url}")
    path_style = parts.path.strip("/").split("/")[0]
    return path_style or parts.hostname.split(".")[0]


def service_client(account_url: str, account_key: str | None) -> BlobServiceClient:
    """A client for the account at `account_url`.

    With an account key (Azurite) it signs requests with the key. Without one it uses the
    process's Entra identity: managed identity in the cloud, where shared-key access is
    disabled, or `az login` on a laptop. Set `AZURE_AUTHORITY_HOST` for Azure Government.
    """
    if account_key is None:
        return BlobServiceClient(account_url, credential=DefaultAzureCredential())
    credential = {"account_name": account_name(account_url), "account_key": account_key}
    return BlobServiceClient(account_url, credential=credential)


def ensure_containers(service: BlobServiceClient, containers: Iterable[str]) -> None:
    """Creates any of `containers` that don't exist. Terraform creates them in the cloud;
    emulators start empty."""
    for container in containers:
        try:
            service.get_container_client(container).create_container()
        except ResourceExistsError:
            continue
        except (HttpResponseError, ServiceRequestError) as exc:
            _raise_translated(exc)


def _props(container: str, blob: BlobProperties) -> BlobProps:
    settings: ContentSettings | None = blob.content_settings
    return BlobProps(
        container=container,
        name=str(blob.name),
        size=int(blob.size),
        etag=str(blob.etag).strip('"'),
        content_type=settings.content_type if settings else None,
        metadata=dict(blob.metadata or {}),
        copy_pending=blob.copy.status == "pending",
    )


def _quoted(etag: str) -> str:
    """Etags travel quoted in conditional headers; the port passes them bare."""
    return f'"{etag}"'


def _raise_translated(exc: HttpResponseError | ServiceRequestError) -> NoReturn:
    """Re-raises an SDK error as the port's error type."""
    network = isinstance(exc, ServiceRequestError | ServiceResponseError)
    status = (exc.status_code or 0) if isinstance(exc, HttpResponseError) else 0
    if network or status in TRANSIENT_STATUS:
        raise TransientStorageError(str(exc)) from exc
    raise StorageError(str(exc)) from exc


class AzureBlobStore:
    def __init__(self, service: BlobServiceClient):
        self._service = service

    @classmethod
    def from_connection_string(cls, connection_string: str) -> "AzureBlobStore":
        return cls(BlobServiceClient.from_connection_string(connection_string))

    def put(
        self,
        container: str,
        name: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobProps:
        blob = self._service.get_blob_client(container, name)
        try:
            blob.upload_blob(
                data,
                overwrite=True,
                metadata=metadata or {},
                content_settings=ContentSettings(content_type=content_type),
            )
            return _props(container, blob.get_blob_properties())
        except (HttpResponseError, ServiceRequestError) as exc:
            _raise_translated(exc)

    def head(self, container: str, name: str) -> BlobProps | None:
        try:
            blob = self._service.get_blob_client(container, name)
            return _props(container, blob.get_blob_properties())
        except ResourceNotFoundError:
            return None
        except (HttpResponseError, ServiceRequestError) as exc:
            _raise_translated(exc)

    def read_range(self, container: str, name: str, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        blob = self._service.get_blob_client(container, name)
        try:
            return blob.download_blob(offset=offset, length=length).readall()
        except ResourceNotFoundError as exc:
            raise BlobNotFound(f"{container}/{name}") from exc
        except HttpResponseError as exc:
            if exc.status_code == RANGE_NOT_SATISFIABLE:  # range starts at or past the end
                return b""
            _raise_translated(exc)
        except ServiceRequestError as exc:
            _raise_translated(exc)

    def delete(self, container: str, name: str, *, if_match: str | None = None) -> None:
        blob = self._service.get_blob_client(container, name)
        try:
            blob.delete_blob(if_match=_quoted(if_match) if if_match else None)
        except ResourceNotFoundError:
            return
        except HttpResponseError as exc:
            if exc.status_code == PRECONDITION_FAILED:
                raise BlobChanged(f"{container}/{name} is not {if_match}") from exc
            _raise_translated(exc)
        except ServiceRequestError as exc:
            _raise_translated(exc)

    def copy(
        self,
        source: tuple[str, str],
        destination: tuple[str, str],
        *,
        if_match: str,
        metadata: dict[str, str] | None = None,
    ) -> BlobProps:
        src = self._service.get_blob_client(*source)
        dst = self._service.get_blob_client(*destination)
        try:
            dst.start_copy_from_url(
                src.url,
                metadata=metadata,
                source_if_match=_quoted(if_match),
                if_none_match="*",  # never overwrite: raw originals are immutable (ING-3)
            )
        except ResourceExistsError as exc:
            raise BlobExists(f"{destination}") from exc
        except ResourceNotFoundError as exc:
            raise BlobChanged(f"{source} is gone") from exc
        except HttpResponseError as exc:
            if exc.status_code == PRECONDITION_FAILED:
                # Which condition failed isn't reported consistently (Azurite and Azure use
                # different error codes), so look: a taken destination is the overwrite refusal.
                if self.head(*destination) is not None:
                    raise BlobExists(f"{destination}") from exc
                raise BlobChanged(f"{source} is not {if_match}") from exc
            _raise_translated(exc)
        except ServiceRequestError as exc:
            _raise_translated(exc)
        return self._await_copy(destination)

    def _await_copy(self, destination: tuple[str, str]) -> BlobProps:
        for _ in range(MAX_COPY_POLLS):
            props = self.head(*destination)
            if props is None:
                raise StorageError(f"copy to {destination} vanished")
            if not props.copy_pending:
                return props
            time.sleep(COPY_POLL_SECONDS)
        raise TransientStorageError(f"copy to {destination} still running")

    def list(self, container: str, prefix: str = "") -> Iterator[BlobProps]:
        client = self._service.get_container_client(container)
        try:
            for blob in client.list_blobs(name_starts_with=prefix or None, include=["metadata"]):
                yield _props(container, blob)
        except (HttpResponseError, ServiceRequestError) as exc:
            _raise_translated(exc)


class AzureBlobSigner:
    """Signs per-blob SAS URLs: with the account key against Azurite, with a user-delegation
    key from the service's Entra identity in the cloud."""

    def __init__(
        self,
        service: BlobServiceClient,
        clock: Clock,
        *,
        account_key: str | None = None,
        public_url: str | None = None,
    ):
        if not service.account_name:
            raise ValueError(f"no storage account name for {service.url}; use service_client()")
        self._service = service
        self._account_name = service.account_name
        self._clock = clock
        self._account_key = account_key
        # Browsers may reach storage on a different host than the API does (Compose).
        self._public_url = (public_url or service.url).rstrip("/")
        self._delegation: UserDelegationKey | None = None
        self._delegation_expiry: datetime | None = None

    def upload_url(self, container: str, name: str, *, expires_at: datetime) -> str:
        token = generate_blob_sas(
            account_name=self._account_name,
            container_name=container,
            blob_name=name,
            account_key=self._account_key,
            user_delegation_key=None if self._account_key else self._delegation_key(expires_at),
            permission=UPLOAD_PERMISSION,
            expiry=expires_at,
        )
        return f"{self._public_url}/{quote(container)}/{quote(name)}?{token}"

    def _delegation_key(self, expires_at: datetime) -> UserDelegationKey:
        if self._delegation and self._delegation_expiry and self._delegation_expiry >= expires_at:
            return self._delegation
        now = self._clock.now()
        key_expiry = now + DELEGATION_KEY_LIFETIME
        if expires_at > key_expiry:
            raise ValueError(f"cannot sign a URL valid beyond {DELEGATION_KEY_LIFETIME}")
        try:
            key = self._service.get_user_delegation_key(now - CLOCK_SKEW, key_expiry)
        except (HttpResponseError, ServiceRequestError) as exc:
            _raise_translated(exc)
        self._delegation, self._delegation_expiry = key, key_expiry
        return key
