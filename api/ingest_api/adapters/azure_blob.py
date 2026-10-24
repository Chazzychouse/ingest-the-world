"""`BlobStore` over Azure Blob Storage (and Azurite)."""

from __future__ import annotations

from collections.abc import Iterator

from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.storage.blob import BlobServiceClient, ContentSettings

from ingest_api.ports import BlobNotFound, BlobProps, TransientStorageError


def _props(container: str, blob) -> BlobProps:
    settings = blob.content_settings
    return BlobProps(
        container=container,
        name=blob.name,
        size=blob.size,
        etag=blob.etag.strip('"'),
        content_type=settings.content_type if settings else None,
        metadata=dict(blob.metadata or {}),
    )


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, ServiceRequestError | ServiceResponseError):
        return TransientStorageError(str(exc))
    if isinstance(exc, HttpResponseError) and (exc.status_code or 0) in (408, 429, 500, 503):
        return TransientStorageError(str(exc))
    return exc


class AzureBlobStore:
    def __init__(self, service: BlobServiceClient):
        self._service = service

    @classmethod
    def from_connection_string(cls, connection_string: str) -> AzureBlobStore:
        return cls(BlobServiceClient.from_connection_string(connection_string))

    def put(self, container, name, data, *, content_type=None, metadata=None) -> BlobProps:
        blob = self._service.get_blob_client(container, name)
        try:
            blob.upload_blob(
                data,
                overwrite=True,
                metadata=metadata or {},
                content_settings=ContentSettings(content_type=content_type),
            )
            return _props(container, blob.get_blob_properties())
        except Exception as exc:
            raise _translate(exc) from exc

    def head(self, container, name) -> BlobProps | None:
        try:
            return _props(
                container, self._service.get_blob_client(container, name).get_blob_properties()
            )
        except ResourceNotFoundError:
            return None
        except Exception as exc:
            raise _translate(exc) from exc

    def read_range(self, container, name, offset, length) -> bytes:
        if length <= 0:
            return b""
        blob = self._service.get_blob_client(container, name)
        try:
            return blob.download_blob(offset=offset, length=length).readall()
        except ResourceNotFoundError as exc:
            raise BlobNotFound(f"{container}/{name}") from exc
        except HttpResponseError as exc:
            if exc.status_code == 416:  # range starts at or past the end
                return b""
            raise _translate(exc) from exc
        except Exception as exc:
            raise _translate(exc) from exc

    def delete(self, container, name) -> None:
        try:
            self._service.get_blob_client(container, name).delete_blob()
        except ResourceNotFoundError:
            pass
        except Exception as exc:
            raise _translate(exc) from exc

    def list(self, container, prefix="") -> Iterator[BlobProps]:
        client = self._service.get_container_client(container)
        try:
            for blob in client.list_blobs(name_starts_with=prefix or None, include=["metadata"]):
                yield _props(container, blob)
        except Exception as exc:
            raise _translate(exc) from exc
