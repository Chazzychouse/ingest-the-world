import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ingest_api.auth import AuthError, OidcVerifier, verifier_from_settings
from ingest_api.routes import collections, identity, meta
from ingest_api.settings import Settings, get_settings


def create_app(settings: Settings | None = None, verifier: OidcVerifier | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        pool.open(wait=True, timeout=30)
        app.state.pool = pool
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="Ingest the World", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.verifier = verifier or verifier_from_settings(settings)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.exception_handler(AuthError)
    async def unauthorized(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            {"detail": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    app.include_router(meta.router)
    app.include_router(identity.router)
    app.include_router(collections.router)
    return app
