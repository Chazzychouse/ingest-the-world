"""The tiler's composition root: the only place its adapters and routes are wired together."""

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ingest_api import previews, tilesets, wiring
from ingest_api.ports import BlobNotFound, BlobStore, Clock, StorageError, SystemClock
from ingest_api.settings import Settings, get_settings
from ingest_api.tiler import raster, routes
from ingest_api.tiler.scenes import Archives


class NoPreviewKey(Exception):
    """The tiler was started without `INGEST_PREVIEW_KEY`, so it could check no token."""


def create_app(
    settings: Settings | None = None,
    *,
    blobs: BlobStore | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.preview_key is None:
        raise NoPreviewKey("set INGEST_PREVIEW_KEY, the key the API signs preview tokens with")
    app = FastAPI(title="Ingest the World tiler", version="0.1.0")
    blobs = blobs or wiring.blob_reader(settings)
    app.state.runtime = routes.Runtime(
        key=settings.preview_key.get_secret_value(),
        blobs=blobs,
        clock=clock or SystemClock(),
        archives=Archives(blobs),
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET"],
            allow_headers=["Authorization"],
        )

    @app.exception_handler(BlobNotFound)
    async def gone(request: Request, __: BlobNotFound) -> JSONResponse:
        # The token's blob was replaced or removed since it was minted: nothing to show.
        scene = request.url.path.startswith(previews.SCENES_PATH)
        detail = routes.SCENE_NOT_FOUND if scene else routes.PREVIEW_NOT_FOUND
        return JSONResponse({"detail": detail}, status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(raster.Undrawable)
    async def undrawable(_: Request, exc: raster.Undrawable) -> JSONResponse:
        return JSONResponse(
            {"detail": f"can't draw this: {exc}"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(tilesets.Malformed)
    async def unreadable(_: Request, exc: tilesets.Malformed) -> JSONResponse:
        return JSONResponse(
            {"detail": f"can't read this scene: {exc}"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(StorageError)
    async def storage_unavailable(_: Request, __: StorageError) -> JSONResponse:
        return JSONResponse(
            {"detail": "storage unavailable"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "5"},
        )

    app.include_router(routes.router)
    app.include_router(routes.scenes_router)
    return app
