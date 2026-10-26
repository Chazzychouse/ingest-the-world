"""Takes messages off the ingest and derive queues and settles each by what came of its work.

    success                          complete
    Busy, StorageError, DB outage    back off, then abandon: redelivered at once
      ... on the last delivery       give the work up (rejected, or failed), then complete
    not a message this queue takes   dead-letter at once: retrying can't help

Any other exception is a bug; it propagates and stops the worker, and the message comes back
when its lock expires. A message that dead-letters that way, or because giving up failed too,
is swept from the dead-letter queue later, and its work given up there (ING-2).
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum

import psycopg
from psycopg import Connection
from psycopg.rows import TupleRow

from ingest_api.events import BlobCreated, InvalidEvent, parse_blob_created
from ingest_api.items.models import DeriveReason
from ingest_api.ports import Clock, Container, LockLost, Message, Queue, StorageError
from ingest_api.uploads.models import Reason
from ingest_api.worker import derive, jobs
from ingest_api.worker.derive import Deriver
from ingest_api.worker.outbox import DeriveRequest, InvalidRequest, parse_derive
from ingest_api.worker.pipeline import Busy, Pipeline, give_up, process

log = logging.getLogger("ingest.worker")

type Connect = Callable[[], AbstractContextManager[Connection[TupleRow]]]

INVALID_MESSAGE = "InvalidMessage"
RETRYABLE = (Busy, StorageError, psycopg.OperationalError)


class Settlement(StrEnum):
    COMPLETE = "complete"
    ABANDON = "abandon"
    DEAD_LETTER = "dead-letter"


@dataclass(frozen=True, slots=True)
class Retry:
    """How failed attempts are retried. `max_deliveries` is the queue's own limit, so the last
    delivery is known before the broker dead-letters it."""

    max_deliveries: int
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0

    def backoff(self, delivery: int) -> float:
        """Doubles with each delivery, up to the cap."""
        return min(self.backoff_seconds * 2 ** min(delivery - 1, 16), self.max_backoff_seconds)


@dataclass(frozen=True, slots=True)
class Work:
    """What one message asks for, in the shape the runner retries."""

    label: str
    run: Callable[[Connection[TupleRow]], object]
    give_up: Callable[[Connection[TupleRow]], object]  # ends it for good, without the work
    note: Callable[[Connection[TupleRow], str], None]  # records a failed attempt for operators


def _attempt(
    message: Message, work: Work, connect: Connect, clock: Clock, retry: Retry
) -> Settlement:
    try:
        with connect() as conn:
            result = work.run(conn)
    except RETRYABLE as exc:
        _note(connect, work, exc)
        if message.delivery_count >= retry.max_deliveries:
            log.warning("giving up %s: %s", work.label, exc)
            return _give_up(connect, work)
        log.info("retrying %s: %s", work.label, exc)
        clock.sleep(retry.backoff(message.delivery_count))
        return Settlement.ABANDON
    log.info("%s %s", work.label, result)
    return Settlement.COMPLETE


def _give_up(connect: Connect, work: Work) -> Settlement:
    try:
        with connect() as conn:
            result = work.give_up(conn)
    except RETRYABLE as exc:
        # Released, so it dead-letters; the sweep gives it up later.
        log.warning("could not give up %s yet: %s", work.label, exc)
        return Settlement.ABANDON
    log.info("%s %s", work.label, result)
    return Settlement.COMPLETE


def _note(connect: Connect, work: Work, exc: Exception) -> None:
    """Notes the failure for operators. If the database is what failed, the log line is all
    there is."""
    try:
        with connect() as conn:
            work.note(conn, f"{type(exc).__name__}: {exc}")
    except psycopg.Error as db_exc:
        log.warning("could not record the error on %s: %s", work.label, db_exc)


# -- ingest -----------------------------------------------------------------------------------


def _landing_event(message: Message) -> BlobCreated | None:
    """The message's blob event, or None if it isn't a BlobCreated for landing."""
    try:
        event = parse_blob_created(message.body)
    except InvalidEvent as exc:
        log.warning("message %s is not a blob event: %s", message.id, exc)
        return None
    if event.container != Container.LANDING:
        log.warning("message %s is for %s, not landing", message.id, event.container)
        return None
    return event


def _ingest(pipeline: Pipeline, event: BlobCreated, reason: Reason) -> Work:
    """Processing a version, which is rejected with `reason` if it has to be given up."""

    def note(conn: Connection[TupleRow], error: str) -> None:
        jobs.record_error(conn, event.name, event.etag, error, now=pipeline.clock.now())

    return Work(
        f"{event.name}@{event.etag}",
        run=lambda conn: process(conn, pipeline, event),
        give_up=lambda conn: give_up(conn, pipeline, event, reason),
        note=note,
    )


def handle(message: Message, connect: Connect, pipeline: Pipeline, retry: Retry) -> Settlement:
    event = _landing_event(message)
    if event is None:
        return Settlement.DEAD_LETTER
    work = _ingest(pipeline, event, Reason.RETRIES_EXHAUSTED)
    return _attempt(message, work, connect, pipeline.clock, retry)


def sweep(message: Message, connect: Connect, pipeline: Pipeline) -> Settlement:
    """Settles a dead letter: its version, if it names one, is given up (ING-2). Completing it
    removes it from the dead-letter queue; abandoning leaves it for the next sweep."""
    event = _landing_event(message)
    if event is None:
        return Settlement.COMPLETE  # no version to end: an invalid event or unreadable body
    return _give_up(connect, _ingest(pipeline, event, Reason.DEAD_LETTERED))


# -- derive -----------------------------------------------------------------------------------


def _derive_request(message: Message) -> DeriveRequest | None:
    try:
        return parse_derive(message.body)
    except InvalidRequest as exc:
        log.warning("message %s is not a derive request: %s", message.id, exc)
        return None


def _derive(deriver: Deriver, request: DeriveRequest, reason: DeriveReason) -> Work:
    """Deriving an item, whose unfinished derivatives fail with `reason` if it has to be given
    up."""
    item_id = request.item_id

    def note(conn: Connection[TupleRow], error: str) -> None:
        jobs.record_derive_error(conn, item_id, error, now=deriver.clock.now())

    return Work(
        f"derive {item_id}",
        run=lambda conn: derive.derive(conn, deriver, item_id),
        give_up=lambda conn: derive.give_up(conn, deriver, item_id, reason),
        note=note,
    )


def handle_derive(message: Message, connect: Connect, deriver: Deriver, retry: Retry) -> Settlement:
    request = _derive_request(message)
    if request is None:
        return Settlement.DEAD_LETTER
    work = _derive(deriver, request, DeriveReason.RETRIES_EXHAUSTED)
    return _attempt(message, work, connect, deriver.clock, retry)


def sweep_derive(message: Message, connect: Connect, deriver: Deriver) -> Settlement:
    request = _derive_request(message)
    if request is None:
        return Settlement.COMPLETE
    return _give_up(connect, _derive(deriver, request, DeriveReason.DEAD_LETTERED))


# -- queues -----------------------------------------------------------------------------------


def settle(queue: Queue, message: Message, settlement: Settlement) -> None:
    try:
        match settlement:
            case Settlement.COMPLETE:
                queue.complete(message)
            case Settlement.ABANDON:
                queue.abandon(message)
            case Settlement.DEAD_LETTER:
                queue.dead_letter(message, reason=INVALID_MESSAGE)
    except LockLost:
        # The work is done or will be redone on redelivery; either way it is idempotent (ING-1).
        log.info("lock on message %s expired before it was settled", message.id)


def _take(queue: Queue, messages: list[Message], handle: Callable[[Message], Settlement]) -> int:
    for message in messages:
        settle(queue, message, handle(message))
    return len(messages)


def run_once(
    queue: Queue, connect: Connect, pipeline: Pipeline, *, retry: Retry, batch: int
) -> int:
    """Handles one batch of blob events; returns how many messages it took."""
    return _take(queue, queue.receive(batch), lambda m: handle(m, connect, pipeline, retry))


def sweep_once(queue: Queue, connect: Connect, pipeline: Pipeline, *, batch: int) -> int:
    """Sweeps one batch of the ingest queue's dead letters; returns how many it took."""
    letters = queue.receive_dead_letters(batch)
    return _take(queue, letters, lambda m: sweep(m, connect, pipeline))


def derive_once(
    queue: Queue, connect: Connect, deriver: Deriver, *, retry: Retry, batch: int
) -> int:
    """Handles one batch of derive requests; returns how many messages it took."""
    return _take(queue, queue.receive(batch), lambda m: handle_derive(m, connect, deriver, retry))


def sweep_derive_once(queue: Queue, connect: Connect, deriver: Deriver, *, batch: int) -> int:
    """Sweeps one batch of the derive queue's dead letters; returns how many it took."""
    letters = queue.receive_dead_letters(batch)
    return _take(queue, letters, lambda m: sweep_derive(m, connect, deriver))
