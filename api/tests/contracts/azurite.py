"""The Azurite connection the contract suites share. Skips cleanly when Azurite isn't running,
unless INGEST_REQUIRE_AZURITE is set (CI, `make check`)."""

import os

import pytest
from azure.storage.blob import BlobServiceClient

from ingest_api import wiring
from ingest_api.adapters.azure_blob import ensure_containers
from ingest_api.ports import StorageError
from tests.conftest import app_settings


def azurite(*containers: str) -> BlobServiceClient:
    """A key-authenticated client for Azurite, with `containers` created if missing."""
    service = wiring.blob_service(app_settings(database_url=""))
    try:
        ensure_containers(service, containers)
    except StorageError as exc:
        if os.environ.get("INGEST_REQUIRE_AZURITE"):
            raise
        pytest.skip(f"Azurite not reachable ({exc}); run `docker compose up azurite`")
    return service
