import ipaddress
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import Connection
from psycopg_pool import ConnectionPool

from ingest_api import audit
from ingest_api.auth import AuthError, OidcVerifier, Principal

_bearer = HTTPBearer(auto_error=False)


def get_pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


def get_conn(pool: Annotated[ConnectionPool, Depends(get_pool)]) -> Iterator[Connection]:
    with pool.connection() as conn:
        yield conn


def _client_ip(request: Request) -> str | None:
    # `client.host` is not always an address (unix sockets, test clients), and the audit
    # column is `inet`.
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


def request_context(request: Request) -> audit.RequestContext:
    return audit.RequestContext(request_id=request.state.request_id, ip=_client_ip(request))


def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise AuthError("missing bearer token")
    verifier: OidcVerifier = request.app.state.verifier
    return verifier.verify(credentials.credentials)


def require_role(minimum: str) -> Callable[..., Principal]:
    """Dependency that admits principals holding `minimum` or a higher role, and audits refusals."""

    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        pool: Annotated[ConnectionPool, Depends(get_pool)],
        ctx: Annotated[audit.RequestContext, Depends(request_context)],
    ) -> Principal:
        if principal.has_role(minimum):
            return principal
        with pool.connection() as conn:
            audit.record(
                conn,
                ctx=ctx,
                principal=principal,
                action=f"{request.method} {request.url.path}",
                outcome="denied",
                detail={"required_role": minimum},
            )
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {minimum}")

    return dependency


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
Conn = Annotated[Connection, Depends(get_conn)]
Ctx = Annotated[audit.RequestContext, Depends(request_context)]
