from dataclasses import dataclass
from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

from ingest_api.auth import Principal

Outcome = Literal["allowed", "denied", "error"]


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    ip: str | None


def record(
    conn: Connection,
    *,
    ctx: RequestContext,
    principal: Principal | None,
    action: str,
    outcome: Outcome,
    target_type: str | None = None,
    target_id: str | None = None,
    marking: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Appends one audit event on `conn`.

    Pass the connection that made the change, so the change and its audit row commit
    or roll back together.
    """
    conn.execute(
        """
        INSERT INTO audit.events
            (actor_sub, actor_name, action, target_type, target_id, marking,
             outcome, ip, request_id, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            principal.subject if principal else None,
            (principal.username or principal.name) if principal else None,
            action,
            target_type,
            target_id,
            marking,
            outcome,
            ctx.ip,
            ctx.request_id,
            Jsonb(detail or {}),
        ),
    )
