"""HTTP for upload sessions: parse, check access, sign, audit, map to status codes.

Every handler ends in exactly one `auditor.allowed(...)` or `raise auditor.refuse(...)`
(ACC-5). A collection or upload the caller can't read answers 404, as if missing (ACC-2);
one they can read but not write answers 403 (ACC-7).
"""

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from ingest_api import authz
from ingest_api.audit import AuditAction, Auditor, Target
from ingest_api.auth import Principal
from ingest_api.collections.routes import collection_target, readable_or_404
from ingest_api.deps import Audit, Conn, CurrentPrincipal, Rt, Runtime
from ingest_api.ports import Container
from ingest_api.uploads import store
from ingest_api.uploads.models import Upload, UploadIn, UploadSession

router = APIRouter(prefix="/uploads", tags=["uploads"])

NOT_FOUND = "upload not found"
NO_WRITE_ACCESS = "you cannot upload into this collection"


def _target(upload_id: UUID) -> Target:
    return Target("upload", str(upload_id))


def _no_write_access(
    auditor: Auditor, action: AuditAction, target: Target, marking: str
) -> HTTPException:
    return auditor.refuse(
        action,
        status_code=status.HTTP_403_FORBIDDEN,
        message=NO_WRITE_ACCESS,
        target=target,
        marking=marking,
    )


def _readable_or_404(
    conn: Conn, auditor: Auditor, action: AuditAction, upload_id: UUID, principal: Principal
) -> store.UploadRow:
    row = store.get_readable(conn, upload_id, principal)
    if row is not None:
        return row
    raise auditor.refuse(
        action,
        status_code=status.HTTP_404_NOT_FOUND,
        message=NOT_FOUND,
        target=_target(upload_id),
        marking=store.marking_of(conn, upload_id),
    )


def _session(runtime: Runtime, row: store.UploadRow) -> UploadSession:
    """The upload with a freshly signed URL for its landing blob, valid until its expiry."""
    url = runtime.signer.upload_url(Container.LANDING, row.blob_name, expires_at=row.url_expires_at)
    return UploadSession(
        upload=Upload.model_validate(row), upload_url=url, expires_at=row.url_expires_at
    )


def _url_lifetime(runtime: Runtime) -> timedelta:
    return timedelta(minutes=runtime.settings.upload_url_minutes)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_upload(
    body: UploadIn, principal: CurrentPrincipal, conn: Conn, auditor: Audit, runtime: Rt
) -> UploadSession:
    action = AuditAction.UPLOAD_CREATE
    collection = readable_or_404(conn, auditor, action, body.collection_id, principal)
    if not authz.can_write(conn, collection.id, principal):
        raise _no_write_access(
            auditor, action, collection_target(collection.id), collection.marking
        )
    upload_id = UUID(runtime.ids.new())
    now = runtime.clock.now()
    store.insert(
        conn,
        upload_id,
        body,
        created_by=principal.subject,
        created_at=now,
        url_expires_at=now + _url_lifetime(runtime),
    )
    row = store.get_readable(conn, upload_id, principal)
    if row is None:
        # The collection was readable a statement ago, in the same transaction.
        raise RuntimeError(f"upload {upload_id} is not readable by its creator")
    session = _session(runtime, row)  # signed before the audit commit: a failure leaves no row
    auditor.allowed(
        action,
        target=_target(upload_id),
        marking=row.marking,
        detail={
            "collection_id": row.collection_id,
            "filename": row.filename,
            "size_bytes": row.size_bytes,
        },
    )
    return session


@router.get("/{upload_id}")
def get_upload(upload_id: UUID, principal: CurrentPrincipal, conn: Conn, auditor: Audit) -> Upload:
    action = AuditAction.UPLOAD_READ
    row = _readable_or_404(conn, auditor, action, upload_id, principal)
    auditor.allowed(action, target=_target(upload_id), marking=row.marking)
    return Upload.model_validate(row)


def _renewal_refusal(
    conn: Conn, auditor: Auditor, row: store.UploadRow, principal: Principal
) -> HTTPException | None:
    """Why `principal` may not renew this upload's URL, as the exception to raise, if any."""
    action = AuditAction.UPLOAD_RENEW
    target = _target(row.id)
    if row.created_by != principal.subject:
        # A signed URL belongs to whoever is moving the bytes; nobody else resumes the transfer.
        return auditor.refuse(
            action,
            status_code=status.HTTP_403_FORBIDDEN,
            message="only the uploader can renew its URL",
            target=target,
            marking=row.marking,
        )
    if not authz.can_write(conn, row.collection_id, principal):
        return _no_write_access(auditor, action, target, row.marking)
    if row.status != "pending":
        return auditor.refuse(
            action,
            status_code=status.HTTP_409_CONFLICT,
            message=f"upload is {row.status}",
            target=target,
            marking=row.marking,
        )
    return None


@router.post("/{upload_id}/url")
def renew_upload_url(
    upload_id: UUID, principal: CurrentPrincipal, conn: Conn, auditor: Audit, runtime: Rt
) -> UploadSession:
    """A new signed URL for an upload still in progress, for transfers that outlast one."""
    action = AuditAction.UPLOAD_RENEW
    row = _readable_or_404(conn, auditor, action, upload_id, principal)
    if refusal := _renewal_refusal(conn, auditor, row, principal):
        raise refusal
    expires_at = runtime.clock.now() + _url_lifetime(runtime)
    store.set_url_expiry(conn, upload_id, expires_at)
    session = _session(runtime, replace(row, url_expires_at=expires_at))
    auditor.allowed(action, target=_target(upload_id), marking=row.marking)
    return session
