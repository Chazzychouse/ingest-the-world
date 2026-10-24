"""The event shim announces each committed landing blob version at least once."""

import random
import uuid
from collections.abc import Iterator

import pytest

from ingest_api.adapters.azure_blob import AzureBlobStore
from ingest_api.event_shim import MAX_BLOBS_PER_POLL, Announced, announce
from ingest_api.events import BlobCreated, parse_blob_created
from ingest_api.ports import BlobProps, BlobStore, Container, Queue, TransientStorageError
from tests.contracts.azurite import azurite
from tests.contracts.servicebus import emulator_queue
from tests.fakes import Faults, MemoryBlobStore, SeededIds, SimClock, SimQueue

ACCOUNT = "http://azurite:10000/devstoreaccount1"
L = Container.LANDING


@pytest.fixture
def store() -> MemoryBlobStore:
    return MemoryBlobStore()


@pytest.fixture
def queue() -> SimQueue:
    return SimQueue(clock=SimClock(), rng=random.Random(0))


def _poll(store: BlobStore, queue: Queue, announced: Announced | None = None) -> dict[str, str]:
    return announce(
        store,
        queue,
        announced or {},
        account_url=ACCOUNT,
        clock=SimClock(),
        ids=SeededIds(random.Random(0)),
    )


def _drain(queue: SimQueue) -> list[BlobCreated]:
    messages = queue.receive(MAX_BLOBS_PER_POLL)
    for message in messages:
        queue.complete(message)
    return [parse_blob_created(m.body) for m in messages]


def test_a_new_blob_is_announced_once(store, queue):
    put = store.put(L, "c/1/a.tif", b"tiff", content_type="image/tiff")
    announced = _poll(store, queue)
    _poll(store, queue, announced)

    assert _drain(queue) == [BlobCreated(L, put.name, put.etag, put.size, "image/tiff")]


def test_a_recommitted_blob_is_announced_again(store, queue):
    store.put(L, "c/1/a.tif", b"v1")
    announced = _poll(store, queue)
    second = store.put(L, "c/1/a.tif", b"v2")
    _poll(store, queue, announced)

    assert [e.etag for e in _drain(queue)][-1] == second.etag
    assert len(queue) == 0


def test_other_containers_are_ignored(store, queue):
    store.put(Container.RAW, "c/1/a.tif", b"x")

    assert _poll(store, queue) == {} and len(queue) == 0


def test_blobs_that_leave_landing_are_forgotten(store, queue):
    store.put(L, "c/1/a.tif", b"x")
    announced = _poll(store, queue)
    store.delete(L, "c/1/a.tif")

    assert _poll(store, queue, announced) == {}


def test_a_failed_poll_loses_nothing(queue):
    flaky = MemoryBlobStore(random.Random(3), Faults(transient=0.5))
    names = [f"c/{n}/f.bin" for n in range(5)]
    for name in names:
        flaky.faults = Faults.none()
        flaky.put(L, name, b"x")
    flaky.faults = Faults(transient=0.5)

    announced: Announced = {}
    for _ in range(50):  # each poll fails with probability 1/2 on its first store call
        try:
            announced = _poll(flaky, queue, announced)
            break
        except TransientStorageError:  # the shim's loop logs these and polls again
            pass
    assert {e.name for e in _drain(queue)} == set(names)


class _OnePrefix(AzureBlobStore):
    """Azurite's landing container, as if it held only one test's blobs: other suites leave
    theirs behind, and announcing those too would make this test slower on every run."""

    def __init__(self, prefix: str):
        super().__init__(azurite(L))
        self._prefix = prefix

    def list(self, container: str, prefix: str = "") -> Iterator[BlobProps]:
        return super().list(container, self._prefix + prefix)


def test_announces_a_blob_committed_to_azurite_onto_the_emulator():
    prefix = f"shim-test/{uuid.uuid4().hex}/"
    store = _OnePrefix(prefix)
    put = store.put(L, f"{prefix}survey 01.tif", b"II*\x00", content_type="image/tiff")
    with emulator_queue("contract") as queue:
        _poll(store, queue)
        [message] = queue.receive()
        queue.complete(message)

    assert parse_blob_created(message.body) == BlobCreated(
        L, put.name, put.etag, put.size, "image/tiff"
    )
