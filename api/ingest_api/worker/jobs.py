"""SQL for pipeline jobs: one row per announced blob version, keyed by (blob name, etag).

`received` is the only in-flight stage. An outcome is written at most once, by a
compare-and-set on `received`, and the database refuses to change it afterwards (ING-2).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow, class_row

from ingest_api.uploads.models import Outcome

Stage = Literal["received", Outcome]


class Reason(StrEnum):
    """Why a version was rejected. Quarantine's reason is the scanner's signature."""

    NO_UPLOAD = "no-upload"  # nothing was uploaded under this name
    UPLOAD_CLOSED = "upload-closed"  # the upload already has an outcome
    SUPERSEDED = "superseded"  # a newer version of the blob replaced this one
    SIZE_MISMATCH = "size-mismatch"  # the bytes don't match the size the upload declared
    CHECKSUM_MISMATCH = "checksum-mismatch"  # nor the checksum it declared


@dataclass(frozen=True, slots=True)
class JobRow:
    blob_name: str
    etag: str
    upload_id: UUID | None
    stage: Stage
    reason: str | None
    attempts: int


def lock(conn: Connection[TupleRow], blob_name: str) -> bool:
    """Takes the session lock that serializes work on one blob name, if it's free. Versions of
    one upload share a name, so no two of them are ever processed at once."""
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (blob_name,)
    ).fetchone()
    return bool(row and row[0])


def unlock(conn: Connection[TupleRow], blob_name: str) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (blob_name,))


def begin(
    conn: Connection[TupleRow],
    blob_name: str,
    etag: str,
    *,
    upload_id: UUID | None,
    now: datetime,
) -> JobRow:
    """The job for this version, created on first delivery and counted on every later one."""
    with conn.cursor(row_factory=class_row(JobRow)) as cur:
        row = cur.execute(
            """
            INSERT INTO ingest.jobs (blob_name, etag, upload_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (blob_name, etag)
                DO UPDATE SET attempts = ingest.jobs.attempts + 1, updated_at = EXCLUDED.updated_at
            RETURNING blob_name, etag, upload_id, stage, reason, attempts
            """,
            (blob_name, etag, upload_id, now, now),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"upsert of job {blob_name}@{etag} returned nothing")
    return row


def finish(
    conn: Connection[TupleRow],
    blob_name: str,
    etag: str,
    outcome: Outcome,
    *,
    reason: str | None,
    now: datetime,
) -> bool:
    """Records the version's outcome if it has none yet; False if it already had one."""
    cur = conn.execute(
        """
        UPDATE ingest.jobs SET stage = %s, reason = %s, last_error = NULL, updated_at = %s
        WHERE blob_name = %s AND etag = %s AND stage = 'received'
        """,
        (outcome, reason, now, blob_name, etag),
    )
    return cur.rowcount == 1


def record_error(
    conn: Connection[TupleRow], blob_name: str, etag: str, error: str, *, now: datetime
) -> None:
    conn.execute(
        """
        UPDATE ingest.jobs SET last_error = %s, updated_at = %s
        WHERE blob_name = %s AND etag = %s
        """,
        (error[:1000], now, blob_name, etag),
    )
