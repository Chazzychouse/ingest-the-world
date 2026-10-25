"""The pipeline for one blob version: decide its outcome, then tidy storage.

    landing ──scan──► infected ──copy──► quarantine/                     quarantined
       │
       └──copy──► raw/ ──hash, classify, extract──► catalog.items        cataloged
       (wrong size, wrong checksum, superseded, no or closed upload)     rejected

Every step can fail midway and be retried from the top on redelivery, so each is idempotent:
the job row (`worker.jobs`) records the outcome once, copies refuse to overwrite and are
recognised by the source etag they carry, and landing is cleared only after the outcome is
committed (ING-1, ING-2, ING-5). Versions of one blob are processed one at a time, under a
lock on its name. The scan comes before anything reaches `raw/` (ING-4).
"""

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial

from psycopg import Connection
from psycopg.rows import TupleRow

from ingest_api.audit import PIPELINE, AuditAction, Auditor, Detail, RequestContext, Target
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
from ingest_api.uploads.models import Reason
from ingest_api.worker import jobs
from ingest_api.worker.classify import HEAD_BYTES, classify
from ingest_api.worker.extract import Budget, Reader, Sandbox, extract
from ingest_api.worker.formats import READERS

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
    sandbox: Sandbox
    # Which reader extracts each classified media type; anything else is `unsupported`.
    readers: Mapping[str, Reader] = field(default_factory=lambda: READERS)


def quarantine_name(event: BlobCreated) -> str:
    """Each quarantined version is kept separately, under its etag."""
    return f"{event.etag}/{event.name}"


type Decide = Callable[
    [Connection[TupleRow], Pipeline, BlobCreated, uploads.LandedUpload | None], jobs.Outcome
]


def process(conn: Connection[TupleRow], pipeline: Pipeline, event: BlobCreated) -> jobs.Outcome:
    """Brings this version to its outcome (or finds the one it already has) and clears it from
    landing. Raises Busy, StorageError or psycopg errors for the caller to retry."""
    return _once(conn, pipeline, event, _decide_first)


def give_up(
    conn: Connection[TupleRow], pipeline: Pipeline, event: BlobCreated, reason: Reason
) -> jobs.Outcome:
    """Ends a version that can't be processed, so nothing is retried forever (ING-2): it is
    rejected with `reason`, or as superseded if a newer version has replaced it, unless it
    already has an outcome. Raises like `process`."""
    return _once(conn, pipeline, event, partial(_abandon, reason=reason))


def _once(
    conn: Connection[TupleRow], p: Pipeline, event: BlobCreated, decide: Decide
) -> jobs.Outcome:
    """Runs `decide` on a version that has no outcome yet, under the lock on its blob name, then
    clears it from landing."""
    if not jobs.lock(conn, event.name):
        raise Busy(event.name)
    try:
        upload = uploads.landed(conn, event.name)
        job = jobs.begin(
            conn, event.name, event.etag, upload_id=upload.id if upload else None, now=p.clock.now()
        )
        conn.commit()
        # A duplicate or redelivery: the outcome stands (ING-1).
        outcome = job.stage if job.stage != "received" else decide(conn, p, event, upload)
        _clear_landing(p.store, event)
        return outcome
    finally:
        conn.rollback()
        jobs.unlock(conn, event.name)
        conn.commit()


def _abandon(
    conn: Connection[TupleRow],
    p: Pipeline,
    event: BlobCreated,
    upload: uploads.LandedUpload | None,
    *,
    reason: Reason,
) -> jobs.Outcome:
    landing = p.store.head(LANDING, event.name)
    if landing is not None and landing.etag != event.etag:
        return _reject(conn, p, event, Reason.SUPERSEDED)
    open_upload = upload if upload is not None and upload.status == "pending" else None
    return _reject(conn, p, event, reason, closes=open_upload)


def _decide_first(
    conn: Connection[TupleRow],
    p: Pipeline,
    event: BlobCreated,
    upload: uploads.LandedUpload | None,
) -> jobs.Outcome:
    if upload is None:
        return _reject(conn, p, event, Reason.NO_UPLOAD)
    if upload.status != "pending":
        return _reject(conn, p, event, Reason.UPLOAD_CLOSED)
    return _decide(conn, p, event, upload)


def _decide(
    conn: Connection[TupleRow], p: Pipeline, event: BlobCreated, upload: uploads.LandedUpload
) -> jobs.Outcome:
    landing = p.store.head(LANDING, event.name)
    if landing is None or landing.etag != event.etag:
        return _reject(conn, p, event, Reason.SUPERSEDED)
    if landing.size != upload.size_bytes:
        return _reject(conn, p, event, Reason.SIZE_MISMATCH, closes=upload)
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
    """Commits the version's outcome, and the upload's when this version decides it, with the
    outcome's audit event."""
    finished = jobs.finish(conn, event.name, event.etag, outcome, reason=reason, now=p.clock.now())
    if closes is not None:
        uploads.close(conn, closes.id, outcome)
    _commit_outcome(conn, event.name, event.etag, outcome, reason=reason, audit=finished)
    return outcome


_ACTIONS: Mapping[jobs.Outcome, AuditAction] = {
    "cataloged": AuditAction.ITEM_CATALOG,
    "quarantined": AuditAction.UPLOAD_QUARANTINE,
    "rejected": AuditAction.UPLOAD_REJECT,
}


def _commit_outcome(
    conn: Connection[TupleRow],
    name: str,
    etag: str,
    outcome: jobs.Outcome,
    *,
    reason: str | None,
    audit: bool,
) -> None:
    """Commits a version's outcome, with exactly one audit event if this call recorded it
    (ACC-5): as the system actor, keyed by the version, marked as its collection."""
    if not audit:
        conn.commit()
        return
    upload = uploads.landed(conn, name)
    auditor = Auditor(conn, RequestContext(request_id=f"{name}@{etag}", ip=None), PIPELINE)
    kind = "item" if outcome == "cataloged" else "upload"
    target = Target(kind, str(upload.id)) if upload else Target("blob", name)
    marking = upload.marking if upload else None
    detail: Detail = {"blob": name, "etag": etag}
    if outcome == "cataloged":
        auditor.allowed(_ACTIONS[outcome], target=target, marking=marking, detail=detail)
        return
    reason = reason or outcome
    auditor.denied(_ACTIONS[outcome], reason=reason, target=target, marking=marking, detail=detail)


def _reject(
    conn: Connection[TupleRow],
    p: Pipeline,
    event: BlobCreated,
    reason: Reason,
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
        return _reject(conn, p, event, Reason.SUPERSEDED)
    except BlobExists:
        pass  # an earlier attempt quarantined this version already
    return _finish(conn, p, event, "quarantined", reason=signature, closes=upload)


def _catalog(
    conn: Connection[TupleRow], p: Pipeline, event: BlobCreated, upload: uploads.LandedUpload
) -> jobs.Outcome:
    raw = _promote(conn, p, event)
    if raw is None:
        return _reject(conn, p, event, Reason.SUPERSEDED)
    sha256 = sha256_of(p.store, RAW, event.name, raw.size)
    if upload.sha256 is not None and sha256 != upload.sha256:
        return _reject(conn, p, event, Reason.CHECKSUM_MISMATCH, closes=upload)
    # Classified and read from the raw copy, which is exactly this version; landing may have
    # moved on.
    head = p.store.read_range(RAW, event.name, 0, HEAD_BYTES)
    found = classify(head, upload.filename, upload.content_type)
    budget = Budget(partial(p.store.read_range, RAW, event.name), event.name, raw.size)
    extraction = extract(p.readers.get(found.media_type), budget, p.sandbox)
    kind = extraction.kind or found.kind
    contents = items.Contents(kind, found.media_type, sha256, raw.size, extraction.metadata)
    try:
        items.insert_from_upload(conn, upload.id, contents, created_at=p.clock.now())
    except items.Duplicate:
        return _reject(conn, p, event, Reason.DUPLICATE, closes=upload)
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
    reason = Reason.SUPERSEDED
    finished = jobs.finish(conn, name, older, "rejected", reason=reason, now=p.clock.now())
    _commit_outcome(conn, name, older, "rejected", reason=reason, audit=finished)


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
