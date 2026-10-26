"""One behavioural contract, run against the in-memory fake and against Azurite.

If the fake drifts from real Blob Storage, the simulator stops telling the truth; this
suite is what keeps them honest. New BlobStore behaviour gets a test here first.
"""

import io
import uuid

import pytest

from ingest_api.adapters.azure_blob import AzureBlobStore
from ingest_api.ports import BlobChanged, BlobExists, BlobNotFound, BlobStore
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


def test_conditional_delete_removes_only_that_version(store, name):
    first = store.put(C, name, b"v1")
    second = store.put(C, name, b"v2")

    with pytest.raises(BlobChanged):
        store.delete(C, name, if_match=first.etag)
    assert store.head(C, name) == second
    store.delete(C, name, if_match=second.etag)
    assert store.head(C, name) is None


def test_copy_makes_an_identical_blob_with_new_metadata(store, name):
    source = store.put(C, name, b"original", content_type="image/tiff", metadata={"a": "1"})

    copied = store.copy((C, name), (C, f"{name}.copy"), if_match=source.etag, metadata={"b": "2"})

    assert store.read_range(C, f"{name}.copy", 0, 100) == b"original"
    assert (copied.size, copied.content_type, copied.metadata) == (8, "image/tiff", {"b": "2"})
    assert store.head(C, f"{name}.copy") == copied


def test_copy_of_a_changed_or_missing_source_is_refused(store, name):
    stale = store.put(C, name, b"v1")
    store.put(C, name, b"v2")

    with pytest.raises(BlobChanged):
        store.copy((C, name), (C, f"{name}.copy"), if_match=stale.etag)
    with pytest.raises(BlobChanged):
        store.copy((C, f"{name}.missing"), (C, f"{name}.copy"), if_match=stale.etag)
    assert store.head(C, f"{name}.copy") is None


def test_copy_never_overwrites(store, name):
    source = store.put(C, name, b"new")
    existing = store.put(C, f"{name}.copy", b"old")

    with pytest.raises(BlobExists):
        store.copy((C, name), (C, f"{name}.copy"), if_match=source.etag)
    assert store.head(C, f"{name}.copy") == existing


def test_create_streams_a_new_blob(store, name):
    created = store.create(
        C, name, io.BytesIO(b"made"), content_type="image/png", metadata={"k": "v"}
    )

    assert store.head(C, name) == created
    assert (created.size, created.content_type, created.metadata) == (4, "image/png", {"k": "v"})
    assert store.read_range(C, name, 0, 10) == b"made"


def test_create_never_overwrites(store, name):
    existing = store.put(C, name, b"first")

    with pytest.raises(BlobExists):
        store.create(C, name, io.BytesIO(b"second"))
    assert store.head(C, name) == existing
    assert store.read_range(C, name, 0, 10) == b"first"
