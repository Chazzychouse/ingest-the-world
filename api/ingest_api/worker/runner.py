"""Takes messages off the ingest queue and settles each by what the pipeline made of it.

    success                          complete
    Busy, StorageError, DB outage    abandon: redelivered, dead-lettered after the queue's limit
    not a BlobCreated for landing/   dead-letter at once: retrying can't help

Any other exception is a bug; it propagates and stops the worker, and the message comes back
when its lock expires.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import StrEnum

import psycopg
from psycopg import Connection
from psycopg.rows import TupleRow

from ingest_api.events import BlobCreated, InvalidEvent, parse_blob_created
from ingest_api.ports import Container, LockLost, Message, Queue, StorageError
from ingest_api.worker import store as jobs
from ingest_api.worker.pipeline import Busy, Pipeline, process

log = logging.getLogger("ingest.worker")

type Connect = Callable[[], AbstractContextManager[Connection[TupleRow]]]

INVALID_EVENT = "InvalidEvent"
RETRYABLE = (Busy, StorageError, psycopg.OperationalError)


class Settlement(StrEnum):
    COMPLETE = "complete"
    ABANDON = "abandon"
    DEAD_LETTER = "dead-letter"


def handle(message: Message, connect: Connect, pipeline: Pipeline) -> Settlement:
    try:
        event = parse_blob_created(message.body)
    except InvalidEvent as exc:
        log.warning("message %s is not a blob event: %s", message.id, exc)
        return Settlement.DEAD_LETTER
    if event.container != Container.LANDING:
        log.warning("message %s is for %s, not landing", message.id, event.container)
        return Settlement.DEAD_LETTER
    try:
        with connect() as conn:
            outcome = process(conn, pipeline, event)
    except RETRYABLE as exc:
        log.info("retrying %s@%s: %s", event.name, event.etag, exc)
        _record_error(connect, pipeline, event, exc)
        return Settlement.ABANDON
    log.info("%s@%s %s", event.name, event.etag, outcome)
    return Settlement.COMPLETE


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


def run_once(queue: Queue, connect: Connect, pipeline: Pipeline, *, batch: int) -> int:
    """Handles one batch; returns how many messages it took."""
    messages = queue.receive(batch)
    for message in messages:
        settle(queue, message, handle(message, connect, pipeline))
    return len(messages)
