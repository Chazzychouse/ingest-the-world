"""Reading audit events in tests, from a high-water mark so earlier history is ignored."""

from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.rows import class_row


@dataclass(frozen=True, slots=True)
class Event:
    action: str
    outcome: str
    marking: str | None
    actor_name: str | None
    target_id: str | None


_SELECT = sql.SQL(
    "SELECT action, outcome, marking, actor_name, target_id FROM audit.events WHERE {}"
)


class AuditLog:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn
        self._mark = 0

    def mark(self) -> None:
        """Forget everything recorded so far; `since()` returns only later events."""
        row = self._conn.execute("SELECT coalesce(max(id), 0) FROM audit.events").fetchone()
        self._mark = row[0] if row else 0

    def since(self) -> list[Event]:
        query = _SELECT.format(sql.SQL("id > %s ORDER BY id"))
        with self._conn.cursor(row_factory=class_row(Event)) as cur:
            return cur.execute(query, (self._mark,)).fetchall()

    def last(self, **where: str) -> Event:
        """The most recent event whose columns equal `where`; LookupError if there is none."""
        clause = sql.SQL(" AND ").join(
            sql.SQL("{} = %s").format(sql.Identifier(column)) for column in where
        )
        query = _SELECT.format(sql.Composed([clause, sql.SQL(" ORDER BY id DESC LIMIT 1")]))
        with self._conn.cursor(row_factory=class_row(Event)) as cur:
            event = cur.execute(query, tuple(where.values())).fetchone()
        if event is None:
            raise LookupError(f"no audit event matching {where}")
        return event
