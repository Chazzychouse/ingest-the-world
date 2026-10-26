"""HTTP for map tiles: footprint views and their vector tiles.

A map draws hundreds of tiles, so tiles are not audited one by one (ACC-5): opening a view is,
with its filters and the highest marking it could show, and its tiles are served only to its
opener until it expires. Each tile runs the search's own query for the caller, so it holds
exactly the readable items matching the view's filters (ACC-9, SRCH-1).
"""

from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from ingest_api.audit import AuditAction, Target
from ingest_api.deps import Audit, Conn, CurrentPrincipal, Rt
from ingest_api.items import store as items
from ingest_api.items.models import Filters
from ingest_api.items.routes import query
from ingest_api.tiles import store
from ingest_api.tiles.models import FootprintView, VectorLayer

router = APIRouter(prefix="/tiles", tags=["tiles"])

VIEW_NOT_FOUND = "footprint view not found"
MVT = "application/vnd.mapbox-vector-tile"
MAX_ZOOM = 22
# A tile holds at most this many footprints, newest first; past it, a map zooms in.
MAX_TILE_FEATURES = 5000
# What each footprint carries besides its shape.
FIELDS = {
    "id": "the item's id",
    "collection": "its collection's id",
    "kind": "what kind of file it is",
    "marking": "its marking's code",
    "title": "its filename",
}

Zoom = Annotated[int, Path(ge=0, le=MAX_ZOOM)]
Index = Annotated[int, Path(ge=0, lt=2**MAX_ZOOM)]


@router.post("/footprints", status_code=status.HTTP_201_CREATED)
def open_footprint_view(
    body: Filters, principal: CurrentPrincipal, conn: Conn, auditor: Audit, runtime: Rt
) -> FootprintView:
    """Opens a view of the footprints of every readable item matching `body`, for a map to
    draw as vector tiles, and returns its TileJSON."""
    view_id = UUID(runtime.ids.new())
    now = runtime.clock.now()
    expires_at = now + timedelta(minutes=runtime.settings.map_session_minutes)
    filters = body.model_dump(mode="json", exclude_none=True)
    store.open_view(conn, view_id, filters, subject=principal.subject, lifetime=(now, expires_at))
    marking = items.highest_marking(conn, principal, query(body, limit=MAX_TILE_FEATURES))
    auditor.allowed(
        AuditAction.FOOTPRINTS_VIEW,
        target=Target("footprint-view", str(view_id)),
        marking=marking,
        detail={"filters": body.model_dump_json(exclude_none=True)},
    )
    return FootprintView(
        tiles=[f"{router.prefix}/footprints/{view_id}/{{z}}/{{x}}/{{y}}.mvt"],
        minzoom=0,
        maxzoom=MAX_ZOOM,
        vector_layers=[VectorLayer(id=items.FOOTPRINT_LAYER, fields=FIELDS)],
        expires_at=expires_at,
    )


def _tile(z: Zoom, x: Index, y: Index) -> items.Tile:
    if x >= 2**z or y >= 2**z:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"no tile {x}, {y} at zoom {z}")
    return items.Tile(z, x, y)


@router.get(
    "/footprints/{view_id}/{z}/{x}/{y}.mvt",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"content": {MVT: {}}, "description": "A Mapbox Vector Tile"},
        status.HTTP_404_NOT_FOUND: {"description": "No live view of the caller's by this id"},
    },
)
def footprint_tile(
    view_id: UUID,
    tile: Annotated[items.Tile, Depends(_tile)],
    principal: CurrentPrincipal,
    conn: Conn,
    runtime: Rt,
) -> Response:
    """One tile of a footprint view the caller opened: their readable items matching its
    filters, drawn in layer `footprints`. Not audited; opening the view was."""
    now = runtime.clock.now()
    filters = store.live_view(conn, view_id, subject=principal.subject, now=now)
    if filters is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, VIEW_NOT_FOUND)
    found = query(Filters.model_validate(filters), limit=MAX_TILE_FEATURES)
    content = items.footprint_tile(conn, principal, found, tile)
    # Per caller, per view, and changing as items arrive or access changes: never cached.
    return Response(content, media_type=MVT, headers={"Cache-Control": "no-store"})
