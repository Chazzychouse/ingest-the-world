"""`BlobStore` over Azure Blob Storage (and Azurite)."""

from collections.abc import Iterator
from typing import NoReturn

from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.storage.blob import BlobProperties, BlobServiceClient, ContentSettings

from ingest_api.ports import BlobNotFound, BlobProps, StorageError, TransientStorageError

# Throttling and server-side failures; anything else is a caller or configuration error.
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RANGE_NOT_SATISFIABLE = 416


def _props(container: str, blob: BlobProperties) -> BlobProps:
    settings: ContentSettings | None = blob.content_settings
    return BlobProps(
        container=container,
        name=str(blob.name),
        size=int(blob.size),
        etag=str(blob.etag).strip('"'),
        content_type=settings.content_type if settings else None,
        metadata=dict(blob.metadata or {}),
    )


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

    def delete(self, container: str, name: str) -> None:
        try:
            self._service.get_blob_client(container, name).delete_blob()
        except ResourceNotFoundError:
            return
        except (HttpResponseError, ServiceRequestError) as exc:
            _raise_translated(exc)

    def list(self, container: str, prefix: str = "") -> Iterator[BlobProps]:
        client = self._service.get_container_client(container)
        try:
            for blob in client.list_blobs(name_starts_with=prefix or None, include=["metadata"]):
                yield _props(container, blob)
        except (HttpResponseError, ServiceRequestError) as exc:
            _raise_translated(exc)
