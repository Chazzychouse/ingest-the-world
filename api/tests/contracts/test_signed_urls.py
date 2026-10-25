"""Signed upload URLs against Azurite: the storage service enforces what ACC-8 promises.

`tests/signed_urls.py` checks what an issued URL says; this suite checks that storage honours
it, by trying everything the URL must not allow.
"""

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from azure.core.exceptions import HttpResponseError
from azure.storage.blob import BlobBlock, BlobClient, BlobServiceClient, ContainerClient

from ingest_api.adapters.azure_blob import AzureBlobSigner, allow_browser_uploads, service_client
from ingest_api.ports import Container, SystemClock
from tests.conftest import TEST_ACCOUNT, TEST_ACCOUNT_KEY
from tests.contracts.azurite import azurite
from tests.signed_urls import check_upload_url

pytestmark = pytest.mark.invariant("ACC-8")
LANDING = Container.LANDING


@pytest.fixture
def service() -> BlobServiceClient:
    return azurite(LANDING)


# As the tests reach Azurite, and as Compose does: by a host name, with browsers given a public
# URL. The SDK parses the account name differently for the two (only IPs and localhost count as
# path-style), so signing must not depend on its parse.
@pytest.fixture(params=["direct", "compose"])
def signer(request: pytest.FixtureRequest, service: BlobServiceClient) -> AzureBlobSigner:
    url = service.url if request.param == "direct" else f"http://azurite:10000/{TEST_ACCOUNT}"
    return AzureBlobSigner(
        service_client(url, TEST_ACCOUNT_KEY),
        SystemClock(),
        account_key=TEST_ACCOUNT_KEY,
        public_url=service.url,
    )


def _sign(signer: AzureBlobSigner, name: str, minutes: float = 10) -> str:
    return signer.upload_url(
        LANDING, name, expires_at=datetime.now(UTC) + timedelta(minutes=minutes)
    )


def _refused(attempt) -> int:
    """The status code storage refused `attempt` with; fails the test if it succeeded."""
    with pytest.raises(HttpResponseError) as refused:
        attempt()
    return refused.value.status_code or 0


@pytest.mark.parametrize(
    "filename", ["plain.tif", "with space.laz", "pct%20literal#hash?q.glb", "ünïcode-測量.obj"]
)
def test_url_writes_exactly_its_blob_in_blocks(service, signer, filename):
    name = f"c/{uuid.uuid4().hex}/{filename}"
    url = _sign(signer, name)
    check_upload_url(url, container=LANDING, blob_name=name, issued_by=datetime.now(UTC))

    blob = BlobClient.from_blob_url(url)
    blob.stage_block("YmxvY2stMQ==", b"hello ")
    blob.stage_block("YmxvY2stMg==", b"world")
    blob.commit_block_list([BlobBlock("YmxvY2stMQ=="), BlobBlock("YmxvY2stMg==")])

    stored = service.get_blob_client(LANDING, name)
    assert stored.download_blob().readall() == b"hello world"


def test_url_cannot_read_delete_or_list(service, signer):
    name = f"c/{uuid.uuid4().hex}/f.bin"
    url = _sign(signer, name)
    blob = BlobClient.from_blob_url(url)
    blob.upload_blob(b"x")

    assert _refused(lambda: blob.download_blob().readall()) == 403
    assert _refused(blob.delete_blob) == 403
    parts = urlsplit(url)
    container_url = urlunsplit(parts._replace(path=parts.path.rsplit("/", 3)[0]))
    assert (
        _refused(lambda: list(ContainerClient.from_container_url(container_url).list_blobs()))
        == 403
    )


def test_url_cannot_write_another_blob(signer):
    url = _sign(signer, f"c/{uuid.uuid4().hex}/mine.bin")
    sibling = url.replace("/mine.bin?", "/theirs.bin?")

    assert _refused(lambda: BlobClient.from_blob_url(sibling).upload_blob(b"x")) == 403


def test_expired_url_is_refused(signer):
    url = _sign(signer, f"c/{uuid.uuid4().hex}/late.bin", minutes=-1)

    assert _refused(lambda: BlobClient.from_blob_url(url).upload_blob(b"x")) == 403


WEB_ORIGIN = "http://web.example"


@pytest.fixture
def browser_uploads(service: BlobServiceClient):
    """Azurite allowing uploads from `WEB_ORIGIN`; the account's rule is put back afterwards,
    since a running stack shares it."""
    before = service.get_service_properties()["cors"]
    allow_browser_uploads(service, [WEB_ORIGIN])
    yield
    service.set_service_properties(cors=before)


def _preflight(url: str, origin: str) -> httpx.Response:
    return httpx.options(
        f"{url}&comp=block&blockid=YmxvY2stMQ%3D%3D",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "x-ms-version",
        },
    )


def test_browsers_upload_only_from_allowed_origins(browser_uploads, signer):
    url = _sign(signer, f"c/{uuid.uuid4().hex}/f.bin")
    allowed = _preflight(url, WEB_ORIGIN)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == WEB_ORIGIN
    assert "access-control-allow-origin" not in _preflight(url, "http://elsewhere.example").headers
