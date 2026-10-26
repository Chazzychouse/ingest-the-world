"""The Auditor: the one way the platform records what it allowed and refused (ACC-5).

Each request gets an Auditor bound to its connection, caller and request id; the pipeline gets
one per blob version, acting as the system actor. Recording an event commits it together with
whatever the request (or the pipeline step) changed, so nothing is reported or acted on before
its audit row is durable.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from fastapi import HTTPException
from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from ingest_api.auth import Principal


class AuditAction(StrEnum):
    COLLECTION_LIST = "collection.list"
    COLLECTION_READ = "collection.read"
    COLLECTION_CREATE = "collection.create"
    GRANT_LIST = "grant.list"
    GRANT_PUT = "grant.put"
    GRANT_DELETE = "grant.delete"
    UPLOAD_CREATE = "upload.create"
    UPLOAD_LIST = "upload.list"
    UPLOAD_READ = "upload.read"
    UPLOAD_RENEW = "upload.renew"
    ITEM_READ = "item.read"
    ITEM_SEARCH = "item.search"
    ASSET_READ = "asset.read"
    ITEM_PREVIEW = "item.preview"
    FOOTPRINTS_VIEW = "footprints.view"
    ROLE_CHECK = "authz.role"
    # The pipeline's outcomes for a landed blob version.
    ITEM_CATALOG = "item.catalog"
    UPLOAD_QUARANTINE = "upload.quarantine"
    UPLOAD_REJECT = "upload.reject"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who an event is about: a signed-in principal, or the platform itself."""

    subject: str
    name: str | None

    @classmethod
    def of(cls, principal: Principal) -> Self:
        return cls(principal.subject, principal.username or principal.name)


# The pipeline acts for the platform, not for whoever uploaded.
PIPELINE = Actor("system:pipeline", "ingest pipeline")


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    ip: str | None


@dataclass(frozen=True, slots=True)
class Target:
    kind: str
    id: str


type Detail = dict[str, str | int | list[str] | None]


class Auditor:
    def __init__(self, conn: Connection[TupleRow], ctx: RequestContext, actor: Actor):
        self._conn = conn
        self._ctx = ctx
        self._actor = actor

    def allowed(
        self,
        action: AuditAction,
        *,
        target: Target | None = None,
        marking: str | None = None,
        detail: Detail | None = None,
    ) -> None:
        """Records a permitted action and commits it with the request's changes."""
        self._record(action, "allowed", target, marking, detail or {})
        self._conn.commit()

    def refuse(
        self,
        action: AuditAction,
        *,
        status_code: int,
        message: str,
        target: Target | None = None,
        marking: str | None = None,
        detail: Detail | None = None,
    ) -> HTTPException:
        """Records and commits a refusal, discarding the request's other changes, and returns
        the exception to raise: `raise auditor.refuse(...)`."""
        self._conn.rollback()
        self.denied(action, reason=message, target=target, marking=marking, detail=detail)
        return HTTPException(status_code, message)

    def denied(
        self,
        action: AuditAction,
        *,
        reason: str,
        target: Target | None = None,
        marking: str | None = None,
        detail: Detail | None = None,
    ) -> None:
        """Records a refused action and commits it with the changes made so far, which record
        the refusal itself (a pipeline rejection is an outcome, not a rollback)."""
        self._record(action, "denied", target, marking, {"reason": reason, **(detail or {})})
        self._conn.commit()

    def _record(
        self,
        action: AuditAction,
        outcome: str,
        target: Target | None,
        marking: str | None,
        detail: Detail,
    ) -> None:
        actor = self._actor
        self._conn.execute(
            """
            INSERT INTO audit.events
                (actor_sub, actor_name, action, target_type, target_id, marking,
                 outcome, ip, request_id, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                actor.subject,
                actor.name,
                action.value,
                target.kind if target else None,
                target.id if target else None,
                marking,
                outcome,
                self._ctx.ip,
                self._ctx.request_id,
                Jsonb(detail),
            ),
        )
