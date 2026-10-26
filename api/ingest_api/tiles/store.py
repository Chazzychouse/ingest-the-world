"""SQL for footprint views: the audited sessions a map's footprint tiles are served under."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

# Expired views are kept a while for whoever reads the audit trail, then cleared as new ones
# open, a bounded batch at a time.
KEEP_EXPIRED = timedelta(days=1)
PURGE_BATCH = 100


def open_view(
    conn: Connection[TupleRow],
    view_id: UUID,
    filters: dict[str, Any],
    *,
    subject: str,
    lifetime: tuple[datetime, datetime],
) -> None:
    created_at, expires_at = lifetime
    conn.execute(
        """
        DELETE FROM catalog.footprint_views WHERE id IN (
            SELECT id FROM catalog.footprint_views WHERE expires_at < %s LIMIT %s
        )
        """,
        (created_at - KEEP_EXPIRED, PURGE_BATCH),
    )
    conn.execute(
        """
        INSERT INTO catalog.footprint_views (id, subject, filters, created_at, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (view_id, subject, Jsonb(filters), created_at, expires_at),
    )


def live_view(
    conn: Connection[TupleRow], view_id: UUID, *, subject: str, now: datetime
) -> dict[str, Any] | None:
    """The filters of the view, if it is `subject`'s and hasn't expired."""
    row = conn.execute(
        """
        SELECT filters FROM catalog.footprint_views
        WHERE id = %s AND subject = %s AND expires_at > %s
        """,
        (view_id, subject, now),
    ).fetchone()
    return None if row is None else row[0]
