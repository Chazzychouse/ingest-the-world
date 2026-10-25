"""Takes messages off the ingest queue and settles each by what the pipeline made of it.

    success                          complete
    Busy, StorageError, DB outage    back off, then abandon: redelivered at once
      ... on the last delivery       give the version up as rejected, then complete
    not a BlobCreated for landing/   dead-letter at once: retrying can't help

Any other exception is a bug; it propagates and stops the worker, and the message comes back
when its lock expires. A message that dead-letters that way, or because giving up failed too,
is swept from the dead-letter queue later: its version is given up there (ING-2).
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
from ingest_api.ports import Container, LockLost, Message, Queue, StorageError
from ingest_api.uploads.models import Reason
from ingest_api.worker import jobs
from ingest_api.worker.pipeline import Busy, Pipeline, give_up, process

log = logging.getLogger("ingest.worker")

type Connect = Callable[[], AbstractContextManager[Connection[TupleRow]]]

INVALID_EVENT = "InvalidEvent"
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


def handle(message: Message, connect: Connect, pipeline: Pipeline, retry: Retry) -> Settlement:
    event = _landing_event(message)
    if event is None:
        return Settlement.DEAD_LETTER
    try:
        with connect() as conn:
            outcome = process(conn, pipeline, event)
    except RETRYABLE as exc:
        _record_error(connect, pipeline, event, exc)
        if message.delivery_count >= retry.max_deliveries:
            log.warning("giving up %s@%s: %s", event.name, event.etag, exc)
            return _give_up(connect, pipeline, event, Reason.RETRIES_EXHAUSTED)
        log.info("retrying %s@%s: %s", event.name, event.etag, exc)
        pipeline.clock.sleep(retry.backoff(message.delivery_count))
        return Settlement.ABANDON
    log.info("%s@%s %s", event.name, event.etag, outcome)
    return Settlement.COMPLETE


def _give_up(
    connect: Connect, pipeline: Pipeline, event: BlobCreated, reason: Reason
) -> Settlement:
    try:
        with connect() as conn:
            outcome = give_up(conn, pipeline, event, reason)
    except RETRYABLE as exc:
        # Released, so it dead-letters; the sweep gives it up later.
        log.warning("could not give up %s@%s yet: %s", event.name, event.etag, exc)
        return Settlement.ABANDON
    log.info("%s@%s %s", event.name, event.etag, outcome)
    return Settlement.COMPLETE


def sweep(message: Message, connect: Connect, pipeline: Pipeline) -> Settlement:
    """Settles a dead letter: its version, if it names one, is given up (ING-2). Completing it
    removes it from the dead-letter queue; abandoning leaves it for the next sweep."""
    event = _landing_event(message)
    if event is None:
        return Settlement.COMPLETE  # no version to end: an invalid event or unreadable body
    return _give_up(connect, pipeline, event, Reason.DEAD_LETTERED)


def _record_error(connect: Connect, pipeline: Pipeline, event: BlobCreated, exc: Exception) -> None:
    """Notes the failure on the job for operators. If the database is what failed, the log line
    is all there is."""
    try:
        with connect() as conn:
            error = f"{type(exc).__name__}: {exc}"
            jobs.record_error(conn, event.name, event.etag, error, now=pipeline.clock.now())
    except psycopg.Error as db_exc:
        log.warning("could not record the error on %s@%s: %s", event.name, event.etag, db_exc)


def settle(queue: Queue, message: Message, settlement: Settlement) -> None:
    try:
        match settlement:
            case Settlement.COMPLETE:
                queue.complete(message)
            case Settlement.ABANDON:
                queue.abandon(message)
            case Settlement.DEAD_LETTER:
                queue.dead_letter(message, reason=INVALID_EVENT)
    except LockLost:
        # The work is done or will be redone on redelivery; either way it is idempotent (ING-1).
        log.info("lock on message %s expired before it was settled", message.id)


def run_once(
    queue: Queue, connect: Connect, pipeline: Pipeline, *, retry: Retry, batch: int
) -> int:
    """Handles one batch; returns how many messages it took."""
    messages = queue.receive(batch)
    for message in messages:
        settle(queue, message, handle(message, connect, pipeline, retry))
    return len(messages)


def sweep_once(queue: Queue, connect: Connect, pipeline: Pipeline, *, batch: int) -> int:
    """Sweeps one batch of dead letters; returns how many it took."""
    letters = queue.receive_dead_letters(batch)
    for letter in letters:
        settle(queue, letter, sweep(letter, connect, pipeline))
    return len(letters)
