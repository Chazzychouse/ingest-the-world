"""Request-scoped dependencies: the runtime, a connection, the caller, their auditor."""

import ipaddress
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from ingest_api.audit import Actor, AuditAction, Auditor, RequestContext
from ingest_api.auth import AuthError, OidcVerifier, Principal, Role
from ingest_api.ports import BlobSigner, Clock, Ids, Pause
from ingest_api.settings import Settings

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Everything a request may use, built once in `app.create_app`."""

    settings: Settings
    verifier: OidcVerifier
    ids: Ids
    clock: Clock
    signer: BlobSigner
    pool: ConnectionPool[Connection[TupleRow]]
    pause: Pause  # waits between a stream's polls without holding a thread


def get_runtime(request: Request) -> Runtime:
    # Starlette types `app.state` as Any; this is the single place it is narrowed.
    return cast(Runtime, request.app.state.runtime)


Rt = Annotated[Runtime, Depends(get_runtime)]


def get_conn(runtime: Rt) -> Iterator[Connection[TupleRow]]:
    with runtime.pool.connection() as conn:
        yield conn


def _client_ip(request: Request) -> str | None:
    # `client.host` is not always an address (unix sockets, test clients), and the audit
    # column is `inet`.
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


def request_context(request: Request) -> RequestContext:
    return RequestContext(request_id=request.state.request_id, ip=_client_ip(request))


def current_principal(
    runtime: Rt,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise AuthError("missing bearer token")
    return runtime.verifier.verify(credentials.credentials)


Conn = Annotated[Connection[TupleRow], Depends(get_conn)]
Ctx = Annotated[RequestContext, Depends(request_context)]
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def get_auditor(conn: Conn, ctx: Ctx, principal: CurrentPrincipal) -> Auditor:
    return Auditor(conn, ctx, Actor.of(principal))


Audit = Annotated[Auditor, Depends(get_auditor)]


def require_role(minimum: Role) -> Callable[..., Principal]:
    """Dependency admitting principals with `minimum` or a higher role; refusals are audited."""

    def dependency(request: Request, principal: CurrentPrincipal, auditor: Audit) -> Principal:
        if principal.has_role(minimum):
            return principal
        raise auditor.refuse(
            AuditAction.ROLE_CHECK,
            status_code=status.HTTP_403_FORBIDDEN,
            message=f"requires role {minimum}",
            detail={"route": f"{request.method} {request.url.path}"},
        )

    return dependency


AdminPrincipal = Annotated[Principal, Depends(require_role(Role.ADMIN))]
