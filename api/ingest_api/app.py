"""Composition root: the only place adapters, the pool and routers are wired together."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

from ingest_api import identity, meta
from ingest_api.auth import AuthError, OidcVerifier, verifier_from_settings
from ingest_api.catalog import routes as catalog
from ingest_api.deps import Runtime
from ingest_api.ports import Ids, UuidIds
from ingest_api.settings import Settings, get_settings

POOL_MAX_SIZE = 10
POOL_OPEN_TIMEOUT_SECONDS = 30


def create_app(
    settings: Settings | None = None,
    verifier: OidcVerifier | None = None,
    ids: Ids | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    runtime = Runtime(
        settings=settings,
        verifier=verifier or verifier_from_settings(settings),
        ids=ids or UuidIds(),
        pool=ConnectionPool(settings.database_url, min_size=1, max_size=POOL_MAX_SIZE, open=False),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        runtime.pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT_SECONDS)
        try:
            yield
        finally:
            runtime.pool.close()

    app = FastAPI(title="Ingest the World", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = request.headers.get("x-request-id") or runtime.ids.new()
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.exception_handler(AuthError)
    async def unauthorized(_: Request, __: AuthError) -> JSONResponse:
        return JSONResponse(
            {"detail": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    for router in (meta.router, identity.router, catalog.router):
        app.include_router(router)
    return app
