"""One behavioural contract, run against the in-memory fake and against Azurite.

If the fake drifts from real Blob Storage, the simulator stops telling the truth; this
suite is what keeps them honest. New BlobStore behaviour gets a test here first.
"""

import uuid

import pytest

from ingest_api.adapters.azure_blob import AzureBlobStore
from ingest_api.ports import BlobNotFound, BlobStore
from tests.contracts.azurite import azurite
from tests.fakes import MemoryBlobStore

C = "contract"


@pytest.fixture(params=["memory", "azurite"])
def store(request: pytest.FixtureRequest) -> BlobStore:
    return MemoryBlobStore() if request.param == "memory" else AzureBlobStore(azurite(C))


@pytest.fixture
def name():
    return f"t/{uuid.uuid4().hex}"


def test_put_then_head(store, name):
    put = store.put(C, name, b"hello", content_type="text/plain", metadata={"k": "v"})
    head = store.head(C, name)

    assert head == put
    assert (head.size, head.content_type, head.metadata) == (5, "text/plain", {"k": "v"})


def test_head_of_missing_blob_is_none(store, name):
    assert store.head(C, name) is None


def test_overwrite_changes_etag(store, name):
    first = store.put(C, name, b"a")
    second = store.put(C, name, b"a")

    assert first.etag != second.etag


@pytest.mark.parametrize(
    ("offset", "length", "expected"),
    [(0, 3, b"abc"), (3, 100, b"defgh"), (8, 4, b""), (20, 4, b""), (2, 0, b"")],
)
def test_read_range_clamps_at_the_end(store, name, offset, length, expected):
    store.put(C, name, b"abcdefgh")

    assert store.read_range(C, name, offset, length) == expected


def test_read_range_of_missing_blob_raises(store, name):
    with pytest.raises(BlobNotFound):
        store.read_range(C, name, 0, 1)


def test_delete_is_idempotent(store, name):
    store.put(C, name, b"x")
    store.delete(C, name)
    store.delete(C, name)

    assert store.head(C, name) is None


def test_list_by_prefix_in_name_order(store, name):
    for suffix in ("b", "a", "c/d"):
        store.put(C, f"{name}/{suffix}", b"x")
    store.put(C, f"{name}-other", b"x")

    assert [b.name for b in store.list(C, f"{name}/")] == [f"{name}/{s}" for s in ("a", "b", "c/d")]
