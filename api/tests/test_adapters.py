"""Pure parts of the adapters, checked without any service running."""

import pytest

from ingest_api.adapters.azure_blob import account_name


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:10000/devstoreaccount1", "devstoreaccount1"),
        ("http://azurite:10000/devstoreaccount1/", "devstoreaccount1"),
        ("https://ingestdata.blob.core.windows.net", "ingestdata"),
        ("https://ingestdata.blob.core.usgovcloudapi.net/", "ingestdata"),
    ],
)
def test_account_name_from_emulator_and_cloud_urls(url, expected):
    assert account_name(url) == expected


def test_account_name_needs_a_url():
    with pytest.raises(ValueError, match="not a storage account URL"):
        account_name("devstoreaccount1")
