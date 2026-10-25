"""SQL for upload sessions. Typed rows out; no HTTP, no auditing.

An upload is readable exactly when its collection is, so reads filter through
`authz.READABLE_IDS` like every other catalog query (ACC-1).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow, class_row

from ingest_api import authz
from ingest_api.auth import Principal
from ingest_api.uploads.models import Outcome, UploadIn, UploadStatus


@dataclass(frozen=True, slots=True)
class UploadRow:
    id: UUID
    collection_id: str
    filename: str
    size_bytes: int
    content_type: str | None
    sha256: str | None
    tags: list[str]
    blob_name: str
    status: UploadStatus
    reason: str | None
    marking: str
    marking_rank: int
    created_by: str
    created_at: datetime
    url_expires_at: datetime


def blob_name(collection_id: str, upload_id: UUID, filename: str) -> str:
    """Where an upload lands. The upload id makes every session's path unique, so a second
    upload of the same file never overwrites the first."""
    return f"{collection_id}/{upload_id.hex}/{filename}"


_SELECT_READABLE = f"""
    SELECT u.id, u.collection_id, u.filename, u.size_bytes, u.content_type, u.sha256, u.tags,
           u.blob_name, u.status, j.reason, c.marking, m.rank AS marking_rank, u.created_by,
           u.created_at, u.url_expires_at
    FROM ingest.uploads u
    -- Why the pipeline decided as it did: the reason of the job whose outcome the upload took.
    LEFT JOIN LATERAL (
        SELECT reason FROM ingest.jobs
        WHERE upload_id = u.id AND stage = u.status
        ORDER BY updated_at DESC LIMIT 1
    ) j ON true
    JOIN catalog.collections c ON c.id = u.collection_id
    JOIN authz.markings m ON m.code = c.marking
    WHERE u.collection_id IN ({authz.READABLE_IDS})
"""  # noqa: S608 - composed from constants only


def insert(
    conn: Connection[TupleRow],
    upload_id: UUID,
    upload: UploadIn,
    *,
    created_by: str,
    created_at: datetime,
    url_expires_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO ingest.uploads
            (id, collection_id, filename, size_bytes, content_type, sha256, tags, blob_name,
             created_by, created_at, url_expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            upload_id,
            upload.collection_id,
            upload.filename,
            upload.size_bytes,
            upload.content_type,
            upload.sha256,
            upload.tags,
            blob_name(upload.collection_id, upload_id, upload.filename),
            created_by,
            created_at,
            url_expires_at,
        ),
    )


def get_readable(
    conn: Connection[TupleRow], upload_id: UUID, principal: Principal
) -> UploadRow | None:
    with conn.cursor(row_factory=class_row(UploadRow)) as cur:
        return cur.execute(
            _SELECT_READABLE + " AND u.id = %s", (*authz.principal_args(principal), upload_id)
        ).fetchone()


def marking_of(conn: Connection[TupleRow], upload_id: UUID) -> str | None:
    """The marking of an upload's collection regardless of access. For audit records only."""
    row = conn.execute(
        """
        SELECT c.marking FROM ingest.uploads u JOIN catalog.collections c ON c.id = u.collection_id
        WHERE u.id = %s
        """,
        (upload_id,),
    ).fetchone()
    return None if row is None else row[0]


def set_url_expiry(conn: Connection[TupleRow], upload_id: UUID, expires_at: datetime) -> None:
    conn.execute(
        "UPDATE ingest.uploads SET url_expires_at = %s WHERE id = %s", (expires_at, upload_id)
    )


@dataclass(frozen=True, slots=True)
class LandedUpload:
    """What the pipeline needs to know about the upload a landed blob belongs to."""

    id: UUID
    collection_id: str
    marking: str  # the collection's, which every event about the upload carries (ACC-5)
    filename: str
    content_type: str | None
    size_bytes: int
    sha256: str | None
    status: UploadStatus


def landed(conn: Connection[TupleRow], name: str) -> LandedUpload | None:
    """The upload whose blob is `landing/{name}`, without access checks: for the pipeline,
    which acts for the platform, not for a caller."""
    with conn.cursor(row_factory=class_row(LandedUpload)) as cur:
        return cur.execute(
            """
            SELECT u.id, u.collection_id, c.marking, u.filename, u.content_type, u.size_bytes,
                   u.sha256, u.status
            FROM ingest.uploads u JOIN catalog.collections c ON c.id = u.collection_id
            WHERE u.blob_name = %s
            """,
            (name,),
        ).fetchone()


def close(conn: Connection[TupleRow], upload_id: UUID, status: Outcome) -> None:
    """Records the pipeline's outcome for a still-pending upload."""
    conn.execute(
        "UPDATE ingest.uploads SET status = %s WHERE id = %s AND status = 'pending'",
        (status, upload_id),
    )
