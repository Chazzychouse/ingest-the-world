"""HTTP for collections and grants: parse, ask the store, audit, map to status codes.

Every handler ends in exactly one `auditor.allowed(...)` or `raise auditor.refuse(...)`
(ACC-5). Unreadable and missing collections get the same 404 (ACC-2).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from ingest_api import authz
from ingest_api.audit import AuditAction, Auditor, Target
from ingest_api.auth import Principal
from ingest_api.collections import store
from ingest_api.collections.models import (
    NO_NUL_PATTERN,
    Collection,
    CollectionIn,
    Grantee,
    GrantIn,
    PrincipalType,
)
from ingest_api.deps import AdminPrincipal, Audit, Conn, CurrentPrincipal

router = APIRouter(prefix="/collections", tags=["collections"])

NOT_FOUND = "collection not found"
GRANT_PATH = "/{collection_id}/grants/{principal_type}/{grantee}"

# Path ids are only looked up, never stored, so a loose bound suffices; ids that don't match
# the creation pattern simply aren't found.
CollectionPathId = Annotated[str, Path(max_length=63, pattern=NO_NUL_PATTERN)]


def collection_target(collection_id: str) -> Target:
    return Target("collection", collection_id)


def grant_key(principal_type: PrincipalType, grantee: Annotated[Grantee, Path()]) -> store.GrantKey:
    return store.GrantKey(principal_type, grantee)


GrantKeyPath = Annotated[store.GrantKey, Depends(grant_key)]


def readable_or_404(
    conn: Conn, auditor: Auditor, action: AuditAction, collection_id: str, principal: Principal
) -> store.CollectionRow:
    """The collection if `principal` may read it; otherwise audits the attempt and raises 404.

    The audit row carries the real marking, for reviewers; the caller learns nothing (ACC-2).
    """
    row = store.get_readable(conn, collection_id, principal)
    if row is not None:
        return row
    raise auditor.refuse(
        action,
        status_code=status.HTTP_404_NOT_FOUND,
        message=NOT_FOUND,
        target=collection_target(collection_id),
        marking=store.marking_of(conn, collection_id),
    )


@router.get("")
def list_collections(principal: CurrentPrincipal, conn: Conn, auditor: Audit) -> list[Collection]:
    rows = store.list_readable(conn, principal)
    auditor.allowed(
        AuditAction.COLLECTION_LIST,
        # Rows come highest marking first, so the first carries the highest disclosed.
        marking=rows[0].marking if rows else None,
        detail={"ids": [r.id for r in rows]},
    )
    return [Collection.model_validate(r) for r in rows]


@router.get("/{collection_id}")
def get_collection(
    collection_id: CollectionPathId, principal: CurrentPrincipal, conn: Conn, auditor: Audit
) -> Collection:
    action = AuditAction.COLLECTION_READ
    row = readable_or_404(conn, auditor, action, collection_id, principal)
    auditor.allowed(action, target=collection_target(collection_id), marking=row.marking)
    return Collection.model_validate(row)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_collection(
    body: CollectionIn, principal: AdminPrincipal, conn: Conn, auditor: Audit
) -> Collection:
    action = AuditAction.COLLECTION_CREATE
    cleared = authz.cleared_for(conn, body.marking, principal)
    if cleared is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown marking")
    if not cleared:
        # An admin can't create data above their own clearance: they couldn't read it back.
        raise auditor.refuse(
            action,
            status_code=status.HTTP_403_FORBIDDEN,
            message="marking exceeds your clearance",
            target=collection_target(body.id),
            marking=body.marking,
        )
    try:
        store.insert(conn, body, created_by=principal.subject)
    except store.AlreadyExists:
        raise auditor.refuse(
            action,
            status_code=status.HTTP_409_CONFLICT,
            message="collection already exists",
            target=collection_target(body.id),
        ) from None
    auditor.allowed(action, target=collection_target(body.id), marking=body.marking)
    # Admins hold every grant, and clearance was checked above, so the row is readable.
    return Collection.model_validate(store.get_readable(conn, body.id, principal))


@router.get("/{collection_id}/grants")
def list_grants(
    collection_id: CollectionPathId, principal: AdminPrincipal, conn: Conn, auditor: Audit
) -> list[store.GrantRow]:
    action = AuditAction.GRANT_LIST
    row = readable_or_404(conn, auditor, action, collection_id, principal)
    grants = store.list_grants(conn, collection_id)
    auditor.allowed(action, target=collection_target(collection_id), marking=row.marking)
    return grants


@router.put(GRANT_PATH)
def put_grant(
    collection_id: CollectionPathId,
    key: GrantKeyPath,
    body: GrantIn,
    principal: AdminPrincipal,
    conn: Conn,
    auditor: Audit,
) -> store.GrantRow:
    # An admin who can't read the collection can't hand out access to it either (ACC-4).
    action = AuditAction.GRANT_PUT
    row = readable_or_404(conn, auditor, action, collection_id, principal)
    store.put_grant(conn, collection_id, key, body.access)
    auditor.allowed(
        action,
        target=collection_target(collection_id),
        marking=row.marking,
        detail={
            "principal_type": key.principal_type,
            "principal": key.principal,
            "access": body.access,
        },
    )
    return store.GrantRow(key.principal_type, key.principal, body.access)


@router.delete(GRANT_PATH, status_code=status.HTTP_204_NO_CONTENT)
def delete_grant(
    collection_id: CollectionPathId,
    key: GrantKeyPath,
    principal: AdminPrincipal,
    conn: Conn,
    auditor: Audit,
) -> None:
    action = AuditAction.GRANT_DELETE
    row = readable_or_404(conn, auditor, action, collection_id, principal)
    deleted = store.delete_grant(conn, collection_id, key)
    auditor.allowed(
        action,
        target=collection_target(collection_id),
        marking=row.marking,
        detail={
            "principal_type": key.principal_type,
            "principal": key.principal,
            "deleted": deleted,
        },
    )
