"""The Azurite connection the contract suites share. Skips cleanly when Azurite isn't running,
unless INGEST_REQUIRE_AZURITE is set (CI, `make check`)."""

import os

import pytest
from azure.core.exceptions import AzureError, ResourceExistsError
from azure.storage.blob import BlobServiceClient

from ingest_api.adapters.azure_blob import service_client
from tests.conftest import app_settings


def azurite(*containers: str) -> BlobServiceClient:
    """A key-authenticated client for Azurite, with `containers` created if missing."""
    settings = app_settings(database_url="")
    key = settings.blob_account_key.get_secret_value() if settings.blob_account_key else None
    service = service_client(settings.blob_account_url, key)
    for container in containers:
        try:
            service.create_container(container, timeout=3)
        except ResourceExistsError:
            pass
        except AzureError as exc:
            if os.environ.get("INGEST_REQUIRE_AZURITE"):
                raise
            pytest.skip(
                f"Azurite not reachable ({type(exc).__name__}); run `docker compose up azurite`"
            )
    return service
