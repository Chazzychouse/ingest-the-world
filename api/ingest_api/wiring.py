"""Builds adapters from `Settings`, for every process the platform runs: the API (`app`), the
worker (`worker.__main__`) and the local event shim (`event_shim`). Each composes itself once at
startup from these, so all three reach storage and the queue the same way."""

import logging

from azure.storage.blob import BlobServiceClient

from ingest_api.adapters.azure_blob import (
    AzureBlobSigner,
    AzureBlobStore,
    ensure_containers,
    service_client,
)
from ingest_api.adapters.service_bus import ServiceBusQueue
from ingest_api.ports import Clock, Container
from ingest_api.settings import Settings


def _account_key(settings: Settings) -> str | None:
    key = settings.blob_account_key
    return key.get_secret_value() if key else None


def blob_service(settings: Settings) -> BlobServiceClient:
    return service_client(settings.blob_account_url, _account_key(settings))


def blob_store(settings: Settings) -> AzureBlobStore:
    """The store, with the platform's containers created if the account is an empty emulator."""
    service = blob_service(settings)
    ensure_containers(service, Container)
    return AzureBlobStore(service)


def blob_signer(settings: Settings, clock: Clock) -> AzureBlobSigner:
    return AzureBlobSigner(
        blob_service(settings),
        clock,
        account_key=_account_key(settings),
        public_url=settings.blob_public_url,
    )


def queue(settings: Settings, *, name: str | None = None) -> ServiceBusQueue:
    """The ingest queue, or the queue `name` on the same namespace. Use as a context manager."""
    connection = settings.queue_connection
    return ServiceBusQueue.connect(
        name or settings.queue_name,
        connection_string=connection.get_secret_value() if connection else None,
        namespace=settings.queue_namespace,
        max_wait_seconds=settings.queue_max_wait_seconds,
        max_lock_renewal_seconds=settings.queue_lock_renewal_seconds,
    )


def configure_logging() -> None:
    """Logging for a daemon's `__main__`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # it logs every HTTP request at INFO
