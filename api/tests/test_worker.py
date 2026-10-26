"""How the worker settles each message: complete, retry, or dead-letter."""

import random
from collections.abc import Iterator

import psycopg
import pytest

from ingest_api.adapters.eicar_scanner import EicarScanner
from ingest_api.ports import BlobProps, Container, TransientStorageError
from ingest_api.uploads.models import Reason
from ingest_api.worker import jobs
from ingest_api.worker.pipeline import Pipeline
from ingest_api.worker.runner import INVALID_MESSAGE, Retry, run_once, sweep_once
from tests.fakes import Faults, MemoryBlobStore, SimClock, SimQueue, event_for
from tests.invariants import check
from tests.test_pipeline import TIFF

RETRY = Retry(max_deliveries=3, backoff_seconds=1.0, max_backoff_seconds=4.0)


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


@pytest.fixture
def queue(clock) -> SimQueue:
    return SimQueue(clock=clock, rng=random.Random(0), max_delivery_count=RETRY.max_deliveries)


@pytest.fixture
def memory() -> MemoryBlobStore:
    return MemoryBlobStore(random.Random(0))


@pytest.fixture
def connect(database_url):
    return lambda: psycopg.connect(database_url)


@pytest.fixture
def pipeline(memory, clock, sandbox) -> Pipeline:
    return Pipeline(store=memory, scanner=EicarScanner(memory), clock=clock, sandbox=sandbox)


@pytest.fixture
def work(connect, queue, pipeline):
    return lambda: run_once(queue, connect, pipeline, retry=RETRY, batch=10)


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
    assert [d.reason for d in queue.dead_letters] == [INVALID_MESSAGE]


def test_an_event_for_another_container_is_dead_lettered(memory, queue, work):
    _announce(queue, memory.put(Container.RAW, "c/1/f.bin", b"x"))

    work()
    assert [d.reason for d in queue.dead_letters] == [INVALID_MESSAGE]


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


class CopyFails(MemoryBlobStore):
    """A store that serves everything except server-side copies: a version can be inspected,
    rejected and cleared, but never promoted."""

    def copy(self, source, destination, *, if_match, metadata=None) -> BlobProps:
        raise TransientStorageError("copies are failing")


def _job(db, name: str) -> tuple[str, str | None]:
    row = db.execute("SELECT stage, reason FROM ingest.jobs WHERE blob_name = %s", (name,))
    return row.fetchone()


@pytest.mark.invariant("ING-2")
def test_a_version_still_failing_on_its_last_delivery_is_rejected(
    connect, queue, clock, sandbox, start, db
):
    store = CopyFails(random.Random(0))
    landed = start(len(TIFF))
    _announce(queue, store.put(Container.LANDING, landed.name, TIFF))
    pipeline = Pipeline(store, EicarScanner(store), clock, sandbox)

    for _ in range(RETRY.max_deliveries):
        run_once(queue, connect, pipeline, retry=RETRY, batch=1)

    assert _job(db, landed.name) == ("rejected", Reason.RETRIES_EXHAUSTED)
    status = db.execute("SELECT status FROM ingest.uploads WHERE id = %s", (landed.upload_id,))
    assert status.fetchone() == ("rejected",)
    assert store.head(Container.LANDING, landed.name) is None
    assert len(queue) == 0 and queue.dead_letters == []
    assert clock.monotonic() == RETRY.backoff(1) + RETRY.backoff(2)  # backed off between tries


@pytest.mark.invariant("ING-2")
def test_a_version_whose_message_dead_letters_is_rejected_by_the_sweep(
    memory, queue, clock, connect, pipeline, db
):
    props = memory.put(Container.LANDING, "c/4/crashed.bin", b"x")
    _announce(queue, props)
    for _ in range(queue.max_delivery_count):  # a worker that dies on every delivery
        queue.receive()
        clock.advance(queue.lock_seconds + 1)
    assert queue.receive() == [] and len(queue.dead_letters) == 1

    assert sweep_once(queue, connect, pipeline, batch=10) == 1
    assert _job(db, props.name) == ("rejected", Reason.DEAD_LETTERED)
    assert queue.dead_letters == []
    assert memory.head(Container.LANDING, props.name) is None


def test_a_dead_letter_naming_no_version_is_swept_away(queue, connect, pipeline):
    queue.send({"not": "an event"})
    message = queue.receive()[0]
    queue.dead_letter(message, reason=INVALID_MESSAGE)

    sweep_once(queue, connect, pipeline, batch=10)
    assert queue.dead_letters == []


@pytest.mark.invariant("ING-1", "ING-2")
def test_sweeping_a_version_that_already_has_an_outcome_changes_nothing(
    memory, queue, work, connect, pipeline, db
):
    props = memory.put(Container.LANDING, "c/5/done.bin", b"x")
    _announce(queue, props)
    work()  # rejected: no upload
    _announce(queue, props)
    [message] = queue.receive()
    queue.dead_letter(message, reason="Whatever")

    sweep_once(queue, connect, pipeline, batch=10)
    assert _job(db, props.name) == ("rejected", Reason.NO_UPLOAD)
    assert queue.dead_letters == []


class SlowStore(MemoryBlobStore):
    """Every ranged read takes simulated time, like hashing a multi-GB file does."""

    def __init__(self, clock: SimClock, seconds_per_read: float):
        super().__init__(random.Random(0))
        self._clock, self._seconds = clock, seconds_per_read

    def read_range(self, container, name, offset, length) -> bytes:
        self._clock.advance(self._seconds)
        return super().read_range(container, name, offset, length)


@pytest.mark.invariant("ING-10")
@pytest.mark.parametrize("renewal", [0.0, 3600.0])
def test_a_slow_job_keeps_its_message_while_locks_are_renewed(
    connect, clock, sandbox, start, db, renewal
):
    queue = SimQueue(
        clock=clock, rng=random.Random(0), lock_seconds=60, max_lock_renewal_seconds=renewal
    )
    store = SlowStore(clock, seconds_per_read=45)  # the job reads several times: minutes
    landed = start(len(TIFF))
    _announce(queue, store.put(Container.LANDING, landed.name, TIFF))
    pipeline = Pipeline(store, EicarScanner(store), clock, sandbox)

    run_once(queue, connect, pipeline, retry=RETRY, batch=1)

    assert _job(db, landed.name) == ("cataloged", None)
    if renewal:
        check("ING-10", len(queue) == 0, "the message was lost to a lapsed lock mid-job")
    else:  # what renewal prevents: the lock lapsed, so the finished job's message comes back
        clock.advance(1)
        [again] = queue.receive()
        assert again.delivery_count == 2
