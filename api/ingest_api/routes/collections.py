from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import Connection, errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from ingest_api import audit
from ingest_api.auth import Principal
from ingest_api.deps import Conn, Ctx, CurrentPrincipal, require_role

router = APIRouter(prefix="/collections", tags=["collections"])

COLUMNS = """
    c.id, c.title, c.description, c.marking, m.label AS marking_label,
    m.banner_color AS marking_color, c.cui_categories, c.dissemination, c.retention,
    c.created_by, c.created_at
"""


class TierStep(BaseModel):
    after_days: int = Field(gt=0)
    tier: Literal["cool", "cold", "archive"]


class Retention(BaseModel):
    retain_days: int | None = Field(default=None, gt=0)
    tiering: list[TierStep] = Field(default_factory=list)
    legal_hold: bool = False


class CollectionIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    marking: str = "UNCLASSIFIED"
    cui_categories: list[str] = Field(default_factory=list)
    dissemination: list[str] = Field(default_factory=list)
    retention: Retention = Field(default_factory=Retention)

    @model_validator(mode="after")
    def cui_details_need_cui(self):
        if self.marking != "CUI" and (self.cui_categories or self.dissemination):
            raise ValueError("cui_categories and dissemination apply only to CUI collections")
        return self


class Collection(CollectionIn):
    marking_label: str
    marking_color: str
    created_by: str
    created_at: str


def _readable_args(principal: Principal) -> tuple:
    return (
        principal.subject,
        list(principal.effective_roles),
        list(principal.groups),
        list(principal.clearances),
    )


def _row(row: dict) -> Collection:
    return Collection(**{**row, "created_at": row["created_at"].isoformat()})


def _fetch(conn: Connection, collection_id: str, principal: Principal) -> dict | None:
    return conn.execute(
        f"""
        SELECT {COLUMNS}
        FROM catalog.collections c JOIN authz.markings m ON m.code = c.marking
        WHERE c.id = %s AND c.id IN (SELECT authz.readable_collections(%s, %s, %s, %s))
        """,  # noqa: S608 - COLUMNS is a constant
        (collection_id, *_readable_args(principal)),
    ).fetchone()


@router.get("")
def list_collections(principal: CurrentPrincipal, conn: Conn) -> list[Collection]:
    rows = conn.execute(
        f"""
        SELECT {COLUMNS}
        FROM catalog.collections c JOIN authz.markings m ON m.code = c.marking
        WHERE c.id IN (SELECT authz.readable_collections(%s, %s, %s, %s))
        ORDER BY m.rank DESC, c.title
        """,  # noqa: S608 - COLUMNS is a constant
        _readable_args(principal),
    ).fetchall()
    return [_row(r) for r in rows]


@router.get("/{collection_id}")
def get_collection(
    collection_id: str, principal: CurrentPrincipal, conn: Conn, ctx: Ctx
) -> Collection:
    row = _fetch(conn, collection_id, principal)
    if row is None:
        # Same response whether the collection is missing or not readable, so markings
        # can't be probed by guessing ids. The audit log records the attempt either way.
        audit.record(
            conn,
            ctx=ctx,
            principal=principal,
            action="collection.read",
            outcome="denied",
            target_type="collection",
            target_id=collection_id,
            detail={"reason": "not found or not readable"},
        )
        conn.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collection not found")
    audit.record(
        conn,
        ctx=ctx,
        principal=principal,
        action="collection.read",
        outcome="allowed",
        target_type="collection",
        target_id=collection_id,
        marking=row["marking"],
    )
    return _row(row)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_collection(
    body: CollectionIn,
    principal: Annotated[Principal, Depends(require_role("Admin"))],
    conn: Conn,
    ctx: Ctx,
) -> Collection:
    cleared = conn.execute(
        "SELECT rank <= authz.clearance_rank(%s) AS ok FROM authz.markings WHERE code = %s",
        (list(principal.clearances), body.marking),
    ).fetchone()
    if cleared is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown marking")
    if not cleared["ok"]:
        # An admin can't create data above their own clearance: they couldn't read it back.
        audit.record(
            conn,
            ctx=ctx,
            principal=principal,
            action="collection.create",
            outcome="denied",
            target_type="collection",
            target_id=body.id,
            marking=body.marking,
            detail={"reason": "marking exceeds clearance"},
        )
        conn.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "marking exceeds your clearance")
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO catalog.collections
                    (id, title, description, marking, cui_categories, dissemination,
                     retention, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    body.id,
                    body.title,
                    body.description,
                    body.marking,
                    body.cui_categories,
                    body.dissemination,
                    Jsonb(body.retention.model_dump()),
                    principal.subject,
                ),
            )
            audit.record(
                conn,
                ctx=ctx,
                principal=principal,
                action="collection.create",
                outcome="allowed",
                target_type="collection",
                target_id=body.id,
                marking=body.marking,
            )
    except errors.UniqueViolation as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "collection already exists") from exc
    # Admins hold every grant, and clearance was checked above, so the row is readable.
    return _row(_fetch(conn, body.id, principal))
