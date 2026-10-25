"""The pipeline for one blob version: decide its outcome, then tidy storage.

    landing ──scan──► infected ──copy──► quarantine/                     quarantined
       │
       └──copy──► raw/ ──hash, classify──► catalog.items                 cataloged
       (wrong size, wrong checksum, superseded, no or closed upload)     rejected

Every step can fail midway and be retried from the top on redelivery, so each is idempotent:
the job row (`worker.store`) records the outcome once, copies refuse to overwrite and are
recognised by the source etag they carry, and landing is cleared only after the outcome is
committed (ING-1, ING-2, ING-5). Versions of one blob are processed one at a time, under a
lock on its name. The scan comes before anything reaches `raw/` (ING-4).
"""

import hashlib
from dataclasses import dataclass

from psycopg import Connection
from psycopg.rows import TupleRow

from ingest_api.events import BlobCreated
from ingest_api.items import store as items
from ingest_api.ports import (
    BlobChanged,
    BlobExists,
    BlobProps,
    BlobStore,
    Clock,
    Container,
    Scanner,
    StorageError,
    TransientStorageError,
)
from ingest_api.uploads import store as uploads
from ingest_api.worker import store as jobs
from ingest_api.worker.classify import HEAD_BYTES, classify

# Blob metadata naming the landing version a copy was made from.
SOURCE_ETAG = "source_etag"
# Read size for hashing a promoted blob; memory per job stays at one chunk.
HASH_CHUNK = 8 * 1024 * 1024

LANDING, RAW, QUARANTINE = Container.LANDING, Container.RAW, Container.QUARANTINE


class Busy(Exception):
    """Another worker is processing a version of this blob; try again later."""


@dataclass(frozen=True, slots=True)
class Pipeline:
    store: BlobStore
    scanner: Scanner
    clock: Clock


def quarantine_name(event: BlobCreated) -> str:
    """Each quarantined version is kept separately, under its etag."""
    return f"{event.etag}/{event.name}"


def process(conn: Connection[TupleRow], pipeline: Pipeline, event: BlobCreated) -> jobs.Outcome:
    """Brings this version to its outcome (or finds the one it already has) and clears it from
    landing. Raises Busy, StorageError or psycopg errors for the caller to retry."""
    if not jobs.lock(conn, event.name):
        raise Busy(event.name)
    try:
        outcome = _decide_once(conn, pipeline, event)
        _clear_landing(pipeline.store, event)
        return outcome
    finally:
        conn.rollback()
        jobs.unlock(conn, event.name)
        conn.commit()


def _decide_once(conn: Connection[TupleRow], p: Pipeline, event: BlobCreated) -> jobs.Outcome:
    upload = uploads.landed(conn, event.name)
    job = jobs.begin(
        conn, event.name, event.etag, upload_id=upload.id if upload else None, now=p.clock.now()
    )
    conn.commit()
    if job.stage != "received":
        return job.stage  # a duplicate or redelivery: the outcome stands (ING-1)
    if upload is None:
        return _reject(conn, p, event, jobs.Reason.NO_UPLOAD)
    if upload.status != "pending":
        return _reject(conn, p, event, jobs.Reason.UPLOAD_CLOSED)
    return _decide(conn, p, event, upload)


def _decide(
    conn: Connection[TupleRow], p: Pipeline, event: BlobCreated, upload: uploads.LandedUpload
) -> jobs.Outcome:
    landing = p.store.head(LANDING, event.name)
    if landing is None or landing.etag != event.etag:
        return _reject(conn, p, event, jobs.Reason.SUPERSEDED)
    if landing.size != upload.size_bytes:
        return _reject(conn, p, event, jobs.Reason.SIZE_MISMATCH, closes=upload)
    verdict = p.scanner.scan(LANDING, event.name)
    if not verdict.clean:
        return _quarantine(conn, p, event, upload, verdict.signature or "unnamed")
    return _catalog(conn, p, event, upload)


def _finish(
    conn: Connection[TupleRow],
    p: Pipeline,
    event: BlobCreated,
    outcome: jobs.Outcome,
    *,
    reason: str | None = None,
    closes: uploads.LandedUpload | None = None,
) -> jobs.Outcome:
    """Commits the version's outcome, and the upload's when this version decides it."""
    jobs.finish(conn, event.name, event.etag, outcome, reason=reason, now=p.clock.now())
    if closes is not None:
        uploads.close(conn, closes.id, outcome)
    conn.commit()
    return outcome


def _reject(
    conn: Connection[TupleRow],
    p: Pipeline,
    event: BlobCreated,
    reason: jobs.Reason,
    *,
    closes: uploads.LandedUpload | None = None,
) -> jobs.Outcome:
    _discard_raw(p.store, event.etag, event.name)
    return _finish(conn, p, event, "rejected", reason=reason, closes=closes)


def _discard_raw(store: BlobStore, etag: str, name: str) -> None:
    """Removes the raw copy an earlier attempt made of version `etag`, which must not outlive
    a rejection (ING-5). A copy of any other version is left alone."""
    raw = store.head(RAW, name)
    if raw is not None and raw.metadata.get(SOURCE_ETAG) == etag:
        store.delete(RAW, name, if_match=raw.etag)


def _quarantine(
    conn: Connection[TupleRow],
    p: Pipeline,
    event: BlobCreated,
    upload: uploads.LandedUpload,
    signature: str,
) -> jobs.Outcome:
    try:
        p.store.copy(
            (LANDING, event.name),
            (QUARANTINE, quarantine_name(event)),
            if_match=event.etag,
            metadata={SOURCE_ETAG: event.etag, "signature": signature},
        )
    except BlobChanged:
        return _reject(conn, p, event, jobs.Reason.SUPERSEDED)
    except BlobExists:
        pass  # an earlier attempt quarantined this version already
    return _finish(conn, p, event, "quarantined", reason=signature, closes=upload)


def _catalog(
    conn: Connection[TupleRow], p: Pipeline, event: BlobCreated, upload: uploads.LandedUpload
) -> jobs.Outcome:
    raw = _promote(conn, p, event)
    if raw is None:
        return _reject(conn, p, event, jobs.Reason.SUPERSEDED)
    sha256 = sha256_of(p.store, RAW, event.name, raw.size)
    if upload.sha256 is not None and sha256 != upload.sha256:
        return _reject(conn, p, event, jobs.Reason.CHECKSUM_MISMATCH, closes=upload)
    # Classified from the raw copy, which is exactly this version; landing may have moved on.
    head = p.store.read_range(RAW, event.name, 0, HEAD_BYTES)
    found = classify(head, upload.filename, upload.content_type)
    contents = items.Contents(found.kind, found.media_type, sha256, raw.size)
    items.insert_from_upload(conn, upload.id, contents, created_at=p.clock.now())
    return _finish(conn, p, event, "cataloged", closes=upload)


def _promote(conn: Connection[TupleRow], p: Pipeline, event: BlobCreated) -> BlobProps | None:
    """This version's copy in `raw/`, made now or by an earlier attempt; None if landing no
    longer holds this version."""
    try:
        return _copy_to_raw(p.store, event)
    except BlobChanged:
        return None
    except BlobExists:
        pass
    raw = p.store.head(RAW, event.name)
    if raw is None or raw.copy_pending:
        raise TransientStorageError(f"raw/{event.name} is not settled yet")
    if raw.metadata.get(SOURCE_ETAG) == event.etag:
        return raw
    _supersede(conn, p, event.name, raw)
    try:
        return _copy_to_raw(p.store, event)  # BlobExists now means another race: retry later
    except BlobChanged:
        return None


def _copy_to_raw(store: BlobStore, event: BlobCreated) -> BlobProps:
    return store.copy(
        (LANDING, event.name),
        (RAW, event.name),
        if_match=event.etag,
        metadata={SOURCE_ETAG: event.etag},
    )


def _supersede(conn: Connection[TupleRow], p: Pipeline, name: str, raw: BlobProps) -> None:
    """`raw/name` holds an older version, promoted but never cataloged (the upload is still
    pending, so no item points at it), whose job stopped midway. Landing holds a newer version,
    so the older one has lost: reject it and free the name."""
    older = raw.metadata.get(SOURCE_ETAG)
    if older is None:
        raise StorageError(f"raw/{name} has no {SOURCE_ETAG}; it was not made by the pipeline")
    p.store.delete(RAW, name, if_match=raw.etag)
    jobs.finish(conn, name, older, "rejected", reason=jobs.Reason.SUPERSEDED, now=p.clock.now())
    conn.commit()


def _clear_landing(store: BlobStore, event: BlobCreated) -> None:
    """Removes this version from landing once it has an outcome. A newer version stays for its
    own event."""
    try:
        store.delete(LANDING, event.name, if_match=event.etag)
    except BlobChanged:
        return


def sha256_of(store: BlobStore, container: str, name: str, size: int) -> str:
    """The blob's SHA-256, streamed one chunk at a time."""
    digest = hashlib.sha256()
    for offset in range(0, size, HASH_CHUNK):
        expected = min(HASH_CHUNK, size - offset)
        chunk = store.read_range(container, name, offset, expected)
        if len(chunk) != expected:
            raise StorageError(f"{container}/{name} is shorter than its {size} bytes")
        digest.update(chunk)
    return digest.hexdigest()
