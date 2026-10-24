from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from psycopg import Connection, errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from ingest_api import audit
from ingest_api.auth import Principal
from ingest_api.deps import Conn, Ctx, CurrentPrincipal, require_role

router = APIRouter(prefix="/collections", tags=["collections"])

COLLECTION_ID = r"^[a-z0-9][a-z0-9-]{1,62}$"
# Postgres text can't hold NUL, so free text rejects it at the edge instead of failing later.
NO_NUL = r"^[^\x00]*$"

COLUMNS = """
    c.id, c.title, c.description, c.marking, m.label AS marking_label,
    m.banner_color AS marking_color, m.rank AS marking_rank, c.cui_categories,
    c.dissemination, c.retention, c.created_by, c.created_at
"""

NOT_FOUND = "collection not found"

AdminPrincipal = Annotated[Principal, Depends(require_role("Admin"))]
CollectionId = Annotated[str, Path(max_length=63, pattern=NO_NUL)]
Grantee = Annotated[str, Path(min_length=1, max_length=256, pattern=NO_NUL)]
Code = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


class TierStep(BaseModel):
    after_days: int = Field(gt=0, le=36500)
    tier: Literal["cool", "cold", "archive"]


class Retention(BaseModel):
    retain_days: int | None = Field(default=None, gt=0, le=36500)
    tiering: list[TierStep] = Field(default_factory=list)
    legal_hold: bool = False


class CollectionIn(BaseModel):
    id: str = Field(pattern=COLLECTION_ID)
    title: str = Field(min_length=1, max_length=200, pattern=NO_NUL)
    description: str = Field(default="", max_length=4000, pattern=NO_NUL)
    marking: Code = "UNCLASSIFIED"
    cui_categories: list[Code] = Field(default_factory=list)
    dissemination: list[Code] = Field(default_factory=list)
    retention: Retention = Field(default_factory=Retention)

    @model_validator(mode="after")
    def cui_details_need_cui(self):
        if self.marking != "CUI" and (self.cui_categories or self.dissemination):
            raise ValueError("cui_categories and dissemination apply only to CUI collections")
        return self


class Collection(CollectionIn):
    marking_label: str
    marking_color: str
    marking_rank: int
    created_by: str
    created_at: str


class GrantIn(BaseModel):
    access: Literal["read", "write", "curate"]


class Grant(GrantIn):
    principal_type: Literal["role", "group", "user"]
    principal: str


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


def _readable_or_404(
    conn: Connection, collection_id: str, principal: Principal, ctx: audit.RequestContext, action
) -> dict:
    """The collection if `principal` may read it; otherwise audits the attempt and raises 404.

    The 404 is the same whether the collection is missing or not readable, so markings can't
    be probed by guessing ids (ACC-2). The audit row carries the real marking for reviewers.
    """
    row = _fetch(conn, collection_id, principal)
    if row is not None:
        return row
    actual = conn.execute(
        "SELECT marking FROM catalog.collections WHERE id = %s", (collection_id,)
    ).fetchone()
    audit.record(
        conn,
        ctx=ctx,
        principal=principal,
        action=action,
        outcome="denied",
        target_type="collection",
        target_id=collection_id,
        marking=actual["marking"] if actual else None,
        detail={"reason": "not found or not readable"},
    )
    conn.commit()
    raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)


@router.get("")
def list_collections(principal: CurrentPrincipal, conn: Conn, ctx: Ctx) -> list[Collection]:
    rows = conn.execute(
        f"""
        SELECT {COLUMNS}
        FROM catalog.collections c JOIN authz.markings m ON m.code = c.marking
        WHERE c.id IN (SELECT authz.readable_collections(%s, %s, %s, %s))
        ORDER BY m.rank DESC, c.title
        """,  # noqa: S608 - COLUMNS is a constant
        _readable_args(principal),
    ).fetchall()
    # Rows are ordered by rank, so the first one carries the highest marking disclosed.
    audit.record(
        conn,
        ctx=ctx,
        principal=principal,
        action="collection.list",
        outcome="allowed",
        target_type="collection",
        marking=rows[0]["marking"] if rows else None,
        detail={"ids": [r["id"] for r in rows]},
    )
    conn.commit()
    return [_row(r) for r in rows]


@router.get("/{collection_id}")
def get_collection(
    collection_id: CollectionId, principal: CurrentPrincipal, conn: Conn, ctx: Ctx
) -> Collection:
    row = _readable_or_404(conn, collection_id, principal, ctx, "collection.read")
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
    conn.commit()
    return _row(row)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_collection(
    body: CollectionIn, principal: AdminPrincipal, conn: Conn, ctx: Ctx
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
        audit.record(
            conn,
            ctx=ctx,
            principal=principal,
            action="collection.create",
            outcome="denied",
            target_type="collection",
            target_id=body.id,
            detail={"reason": "already exists"},
        )
        conn.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "collection already exists") from exc
    conn.commit()
    # Admins hold every grant, and clearance was checked above, so the row is readable.
    return _row(_fetch(conn, body.id, principal))


@router.get("/{collection_id}/grants")
def list_grants(
    collection_id: CollectionId, principal: AdminPrincipal, conn: Conn, ctx: Ctx
) -> list[Grant]:
    row = _readable_or_404(conn, collection_id, principal, ctx, "grant.list")
    grants = conn.execute(
        """
        SELECT principal_type, principal, access FROM authz.collection_grants
        WHERE collection_id = %s ORDER BY principal_type, principal
        """,
        (collection_id,),
    ).fetchall()
    audit.record(
        conn,
        ctx=ctx,
        principal=principal,
        action="grant.list",
        outcome="allowed",
        target_type="collection",
        target_id=collection_id,
        marking=row["marking"],
    )
    conn.commit()
    return [Grant(**g) for g in grants]


@router.put("/{collection_id}/grants/{principal_type}/{grantee}")
def put_grant(
    collection_id: CollectionId,
    principal_type: Literal["role", "group", "user"],
    grantee: Grantee,
    body: GrantIn,
    principal: AdminPrincipal,
    conn: Conn,
    ctx: Ctx,
) -> Grant:
    # An admin who can't read the collection can't hand out access to it either (ACC-4).
    row = _readable_or_404(conn, collection_id, principal, ctx, "grant.put")
    conn.execute(
        """
        INSERT INTO authz.collection_grants (collection_id, principal_type, principal, access)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (collection_id, principal_type, principal)
            DO UPDATE SET access = excluded.access
        """,
        (collection_id, principal_type, grantee, body.access),
    )
    audit.record(
        conn,
        ctx=ctx,
        principal=principal,
        action="grant.put",
        outcome="allowed",
        target_type="collection",
        target_id=collection_id,
        marking=row["marking"],
        detail={"principal_type": principal_type, "principal": grantee, "access": body.access},
    )
    conn.commit()
    return Grant(principal_type=principal_type, principal=grantee, access=body.access)


@router.delete("/{collection_id}/grants/{principal_type}/{grantee}", status_code=204)
def delete_grant(
    collection_id: CollectionId,
    principal_type: Literal["role", "group", "user"],
    grantee: Grantee,
    principal: AdminPrincipal,
    conn: Conn,
    ctx: Ctx,
) -> None:
    row = _readable_or_404(conn, collection_id, principal, ctx, "grant.delete")
    deleted = conn.execute(
        """
        DELETE FROM authz.collection_grants
        WHERE collection_id = %s AND principal_type = %s AND principal = %s
        """,
        (collection_id, principal_type, grantee),
    ).rowcount
    audit.record(
        conn,
        ctx=ctx,
        principal=principal,
        action="grant.delete",
        outcome="allowed",
        target_type="collection",
        target_id=collection_id,
        marking=row["marking"],
        detail={"principal_type": principal_type, "principal": grantee, "deleted": deleted},
    )
    conn.commit()
