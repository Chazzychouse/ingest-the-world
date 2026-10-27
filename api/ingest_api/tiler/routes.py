"""HTTP for previews and scenes: an item's TileJSON and its PNG tiles under its preview token,
and the files of its 3D Tiles (`tileset.json` and what that names) under its scene token.

Every request carries the token as `Authorization: Bearer`. One that is missing, forged,
expired, for the other kind of route or minted for another item answers 404, the same for every
reason, and nothing is read (ACC-9). Requests are not audited: opening the preview or scene was
(ACC-5). A blob that is gone answers 404 too, and one that can't be drawn or read 422
(`tiler.app` maps both).
"""

from dataclasses import dataclass
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi import Path as PathParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from rio_tiler.errors import TileOutsideBounds

from ingest_api import display, previews
from ingest_api.items.store import Tile
from ingest_api.ports import BlobStore, Clock
from ingest_api.tiler import raster
from ingest_api.tiler.scenes import Archives
from ingest_api.tiles.models import RasterTiles
from ingest_api.tiles.routes import tile_at

router = APIRouter(prefix=previews.TILES_PATH, tags=["previews"])
scenes_router = APIRouter(prefix=previews.SCENES_PATH, tags=["scenes"])

PREVIEW_NOT_FOUND = "preview not found"
SCENE_NOT_FOUND = "scene not found"
PNG = "image/png"
MAX_SCENE_PATH = 256
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Everything a request may use, built once in `tiler.app.create_app`."""

    key: str
    blobs: BlobStore
    clock: Clock
    archives: Archives


def _runtime(request: Request) -> Runtime:
    # Starlette types `app.state` as Any; this is the single place it is narrowed.
    return cast(Runtime, request.app.state.runtime)


Rt = Annotated[Runtime, Depends(_runtime)]


def _preview(
    item_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    runtime: Rt,
) -> previews.Preview:
    token = credentials.credentials if credentials else ""
    preview = previews.verify(token, runtime.key, item_id=item_id, now=runtime.clock.now())
    if preview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, PREVIEW_NOT_FOUND)
    return preview


Granted = Annotated[previews.Preview, Depends(_preview)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "No live preview of this item"}
}


def _private(preview: previews.Preview, runtime: Runtime) -> dict[str, str]:
    """Cacheable by the browser that asked, until the token expires; by nothing else."""
    seconds = max(0, int((preview.expires_at - runtime.clock.now()).total_seconds()))
    return {"Cache-Control": f"private, max-age={seconds}"}


@router.get("/{item_id}/tilejson.json", responses=NOT_FOUND)
def tilejson(
    item_id: UUID, preview: Granted, request: Request, response: Response, runtime: Rt
) -> RasterTiles:
    """Where the item's raster lies, and its tiles' URL."""
    extent = raster.extent(runtime.blobs, preview)
    template = str(request.url_for("preview_tile", item_id=item_id, z="{z}", x="{x}", y="{y}"))
    if extent.stretch:
        low, high = extent.stretch
        template += f"?low={low!r}&high={high!r}"
    response.headers.update(_private(preview, runtime))
    return RasterTiles(
        tiles=[template.replace("%7B", "{").replace("%7D", "}")],
        minzoom=extent.minzoom,
        maxzoom=extent.maxzoom,
        bounds=extent.bounds,
    )


@router.get(
    "/{item_id}/{z}/{x}/{y}.png",
    name="preview_tile",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"content": {PNG: {}}, "description": "A PNG tile"},
        status.HTTP_204_NO_CONTENT: {"description": "The raster doesn't reach this tile"},
        **NOT_FOUND,
    },
)
def preview_tile(
    preview: Granted,
    at: Annotated[Tile, Depends(tile_at)],
    runtime: Rt,
    low: Annotated[float | None, Query(allow_inf_nan=False)] = None,
    high: Annotated[float | None, Query(allow_inf_nan=False)] = None,
) -> Response:
    """One tile of the item's raster, its samples stretched between `low` and `high` (as the
    TileJSON's URL gives them) or else over the tile's own."""
    stretch: display.Stretch | None = (low, high) if low is not None and high is not None else None
    try:
        png = raster.tile(runtime.blobs, preview, at, stretch)
    except TileOutsideBounds:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(png, media_type=PNG, headers=_private(preview, runtime))


def _scene(
    item_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    runtime: Rt,
) -> previews.Preview:
    token = credentials.credentials if credentials else ""
    now = runtime.clock.now()
    scene = previews.verify(
        token, runtime.key, item_id=item_id, now=now, audience=previews.SCENE_AUDIENCE
    )
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, SCENE_NOT_FOUND)
    return scene


@scenes_router.get(
    "/{item_id}/{path:path}",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"description": "A file of the item's 3D Tiles"},
        status.HTTP_404_NOT_FOUND: {"description": "No live scene of this item, or no such file"},
    },
)
def scene_file(
    scene: Annotated[previews.Preview, Depends(_scene)],
    path: Annotated[str, PathParam(min_length=1, max_length=MAX_SCENE_PATH)],
    runtime: Rt,
) -> Response:
    """One file of the item's tileset, as its `.3tz` holds it."""
    found = runtime.archives.file(scene, path)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, SCENE_NOT_FOUND)
    return Response(found.data, media_type=found.media_type, headers=_private(scene, runtime))
