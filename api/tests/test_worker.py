"""How the worker settles each message: complete, retry, or dead-letter."""

import random
from collections.abc import Iterator

import psycopg
import pytest

from ingest_api.adapters.eicar_scanner import EicarScanner
from ingest_api.ports import BlobProps, Container
from ingest_api.worker import store as jobs
from ingest_api.worker.pipeline import Pipeline
from ingest_api.worker.runner import INVALID_EVENT, run_once
from tests.fakes import Faults, MemoryBlobStore, SimClock, SimQueue, event_for


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


@pytest.fixture
def queue(clock) -> SimQueue:
    return SimQueue(clock=clock, rng=random.Random(0))


@pytest.fixture
def memory() -> MemoryBlobStore:
    return MemoryBlobStore(random.Random(0))


@pytest.fixture
def work(database_url, memory, queue, clock):
    pipeline = Pipeline(store=memory, scanner=EicarScanner(memory), clock=clock)
    return lambda: run_once(queue, lambda: psycopg.connect(database_url), pipeline, batch=10)


def _announce(queue: SimQueue, props: BlobProps) -> None:
    queue.send(event_for(props))


def test_a_handled_event_is_completed(memory, queue, work):
    _announce(queue, memory.put(Container.LANDING, "c/1/stray.bin", b"x"))

    assert work() == 1
    assert len(queue) == 0 and queue.dead_letters == []


@pytest.mark.parametrize(
    "body",
    [{"eventType": "Microsoft.Storage.BlobDeleted"}, {"not": "an event"}],
)
def test_a_message_that_is_not_a_blob_event_is_dead_lettered(queue, work, body):
    queue.send(body)

    work()
    assert [d.reason for d in queue.dead_letters] == [INVALID_EVENT]


def test_an_event_for_another_container_is_dead_lettered(memory, queue, work):
    _announce(queue, memory.put(Container.RAW, "c/1/f.bin", b"x"))

    work()
    assert [d.reason for d in queue.dead_letters] == [INVALID_EVENT]


def test_a_storage_failure_is_retried_and_noted_on_the_job(memory, queue, work, db):
    props = memory.put(Container.LANDING, "c/2/stray.bin", b"x")
    _announce(queue, props)
    memory.faults = Faults(transient=1.0)

    work()
    [message] = queue.receive()
    assert message.delivery_count == 2  # abandoned, so redelivered at once
    error = db.execute(
        "SELECT last_error FROM ingest.jobs WHERE blob_name = %s", (props.name,)
    ).fetchone()
    assert error is not None and "TransientStorageError" in error[0]


@pytest.fixture
def held_lock(database_url) -> Iterator[str]:
    """Another worker busy with this blob name."""
    name = "c/3/busy.bin"
    with psycopg.connect(database_url, autocommit=True) as other:
        assert jobs.lock(other, name)
        yield name


def test_a_blob_another_worker_holds_is_retried(memory, queue, work, held_lock):
    _announce(queue, memory.put(Container.LANDING, held_lock, b"x"))

    work()
    [message] = queue.receive()
    assert message.delivery_count == 2
    assert memory.head(Container.LANDING, held_lock) is not None
