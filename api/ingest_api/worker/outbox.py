"""The outbox: messages written in the same transaction as the change they announce, then relayed
to their queue.

The pipeline catalogs an item and asks for its derivatives in one commit (ING-11), so neither
happens without the other. The relay sends each unpublished message and then stamps it; a crash
in between sends it again, which the derive stage absorbs (ING-12). Messages never go missing,
but they may arrive twice.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from ingest_api.ports import Queue

DERIVE = "derive"


class InvalidRequest(ValueError):
    """A message that isn't a request this platform can act on."""


@dataclass(frozen=True, slots=True)
class DeriveRequest:
    """Derive whatever this item is missing."""

    item_id: UUID


def request_derive(conn: Connection[TupleRow], item_id: UUID, *, now: datetime) -> None:
    """Asks for the item's derivatives, once the caller's transaction commits."""
    conn.execute(
        "INSERT INTO ingest.outbox (topic, body, created_at) VALUES (%s, %s, %s)",
        (DERIVE, Jsonb({"item_id": str(item_id)}), now),
    )


def parse_derive(body: dict[str, Any]) -> DeriveRequest:
    value = body.get("item_id")
    if not isinstance(value, str):
        raise InvalidRequest("no item_id")
    try:
        return DeriveRequest(UUID(value))
    except ValueError as exc:
        raise InvalidRequest(f"item_id {value!r} is not a UUID") from exc


def relay(conn: Connection[TupleRow], queue: Queue, *, limit: int, now: datetime) -> int:
    """Sends up to `limit` unpublished derive requests, oldest first, then stamps them published
    and commits; returns how many it sent. Rows another relay holds are skipped. If a send
    fails, nothing is stamped and every row in this batch is sent again next time."""
    rows = conn.execute(
        """
        SELECT id, body FROM ingest.outbox
        WHERE published_at IS NULL AND topic = %s
        ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
        """,
        (DERIVE, limit),
    ).fetchall()
    for _, body in rows:
        queue.send(body)
    conn.execute(
        "UPDATE ingest.outbox SET published_at = %s WHERE id = ANY(%s)",
        (now, [row_id for row_id, _ in rows]),
    )
    conn.commit()
    return len(rows)
