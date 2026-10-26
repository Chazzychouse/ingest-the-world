"""SQL for the worker's jobs. A pipeline job is one announced blob version, keyed by (blob name,
etag); a derive job is one derivative of an item, keyed by (item, role, recipe version).

`received` is the only in-flight stage of either. An outcome is written at most once, by a
compare-and-set on `received`, and the database refuses to change it afterwards (ING-2, ING-12).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow, class_row

from ingest_api.uploads.models import Outcome

Stage = Literal["received", Outcome]


@dataclass(frozen=True, slots=True)
class JobRow:
    blob_name: str
    etag: str
    upload_id: UUID | None
    stage: Stage
    reason: str | None
    attempts: int


def lock(conn: Connection[TupleRow], key: str) -> bool:
    """Takes the session lock that serializes work on `key`, if it's free: a blob name, so no two
    versions of one upload are ever processed at once, or `derive_key(item)`."""
    row = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)).fetchone()
    return bool(row and row[0])


def unlock(conn: Connection[TupleRow], key: str) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


def derive_key(item_id: UUID) -> str:
    """The lock key for deriving one item. Blob names never start with `derive:`."""
    return f"derive:{item_id}"


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


DeriveOutcome = Literal["derived", "failed"]
DeriveStage = Literal["received", DeriveOutcome]


@dataclass(frozen=True, slots=True)
class DeriveJobRow:
    item_id: UUID
    role: str
    recipe_version: int
    stage: DeriveStage
    reason: str | None


def begin_derive(
    conn: Connection[TupleRow], item_id: UUID, role: str, version: int, *, now: datetime
) -> DeriveJobRow:
    """The derive job for this derivative, created on first delivery and counted on later ones."""
    with conn.cursor(row_factory=class_row(DeriveJobRow)) as cur:
        row = cur.execute(
            """
            INSERT INTO ingest.derive_jobs (item_id, role, recipe_version, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (item_id, role, recipe_version)
                DO UPDATE SET attempts = ingest.derive_jobs.attempts + 1,
                              updated_at = EXCLUDED.updated_at
            RETURNING item_id, role, recipe_version, stage, reason
            """,
            (item_id, role, version, now, now),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"upsert of derive job {item_id}/{role}/v{version} returned nothing")
    return row


def finish_derive(
    conn: Connection[TupleRow],
    job: DeriveJobRow,
    outcome: DeriveOutcome,
    *,
    reason: str | None,
    now: datetime,
) -> bool:
    """Records the derive job's outcome if it has none yet; False if it already had one."""
    cur = conn.execute(
        """
        UPDATE ingest.derive_jobs SET stage = %s, reason = %s, last_error = NULL, updated_at = %s
        WHERE item_id = %s AND role = %s AND recipe_version = %s AND stage = 'received'
        """,
        (outcome, reason, now, job.item_id, job.role, job.recipe_version),
    )
    return cur.rowcount == 1


def record_derive_error(
    conn: Connection[TupleRow], item_id: UUID, error: str, *, now: datetime
) -> None:
    """Notes the failure on the item's unfinished derive jobs, for operators."""
    conn.execute(
        """
        UPDATE ingest.derive_jobs SET last_error = %s, updated_at = %s
        WHERE item_id = %s AND stage = 'received'
        """,
        (error[:1000], now, item_id),
    )


def unsettle_derive(conn: Connection[TupleRow], item_id: UUID) -> None:
    """Marks the item's derivatives as in hand again, before any job of them begins (ING-13)."""
    conn.execute("DELETE FROM ingest.derive_settled WHERE item_id = %s", (item_id,))


def settle_derive(conn: Connection[TupleRow], item_id: UUID, *, now: datetime) -> None:
    """Marks every derivative of the item as having an outcome (ING-13)."""
    conn.execute(
        """
        INSERT INTO ingest.derive_settled (item_id, settled_at) VALUES (%s, %s)
        ON CONFLICT (item_id) DO UPDATE SET settled_at = EXCLUDED.settled_at
        """,
        (item_id, now),
    )
