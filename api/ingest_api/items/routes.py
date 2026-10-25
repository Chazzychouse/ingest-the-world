"""HTTP for items and search: parse, ask the store, audit, render as STAC.

Every handler ends in exactly one `auditor.allowed(...)` or `raise auditor.refuse(...)`
(ACC-5). An item the caller can't read answers 404, as if missing (ACC-2); a search simply
never returns it (ACC-1). Only cataloged files are items, so nothing quarantined or rejected
can be served (ING-4).
"""

from uuid import UUID

from fastapi import APIRouter, status

from ingest_api.audit import AuditAction, Target
from ingest_api.deps import Audit, Conn, CurrentPrincipal
from ingest_api.items import store
from ingest_api.items.models import (
    FILE_EXTENSION,
    PROJECTION_EXTENSION,
    Cursor,
    ItemCollection,
    ItemProperties,
    Link,
    NextBody,
    NextLink,
    Polygon,
    SearchIn,
    StacItem,
    parse_interval,
)

router = APIRouter(tags=["items"])

NOT_FOUND = "item not found"
SEARCH_PATH = "/search"
# sha2-256's multihash prefix: function code 0x12, digest length 0x20.
SHA256_MULTIHASH = "1220"


def _target(item_id: UUID) -> Target:
    return Target("item", str(item_id))


def _crs(crs: str | None) -> tuple[str | None, str | None]:
    """`proj:code` and `proj:wkt2`: a CRS is named by code when it has one, else by WKT2."""
    if crs is None:
        return None, None
    return (None, crs) if "[" in crs else (crs, None)


def stac_item(row: store.ItemRow) -> StacItem:
    code, wkt2 = _crs(row.crs)
    core = {
        "datetime": row.created_at,
        "created": row.created_at,
        "title": row.filename,
        "keywords": row.tags,
        "ingest:kind": row.kind,
        "ingest:media_type": row.media_type,
        "ingest:marking": row.marking,
        "ingest:marking_rank": row.marking_rank,
        "ingest:extraction": row.extraction,
        "file:size": row.size_bytes,
        "file:checksum": SHA256_MULTIHASH + row.sha256,
        "proj:code": code,
        "proj:wkt2": wkt2,
    }
    # Core fields win over a header that happens to use the same key.
    properties = ItemProperties.model_validate({**row.properties, **core})
    return StacItem(
        stac_extensions=[FILE_EXTENSION, PROJECTION_EXTENSION],
        id=row.id,
        collection=row.collection_id,
        geometry=None if row.footprint is None else Polygon.model_validate(row.footprint),
        bbox=None if row.bbox is None else (row.bbox[0], row.bbox[1], row.bbox[2], row.bbox[3]),
        properties=properties,
        links=[
            Link(rel="self", href=f"/items/{row.id}", type="application/geo+json"),
            Link(
                rel="collection", href=f"/collections/{row.collection_id}", type="application/json"
            ),
        ],
        assets={},
    )


@router.get("/items/{item_id}")
def get_item(item_id: UUID, principal: CurrentPrincipal, conn: Conn, auditor: Audit) -> StacItem:
    action = AuditAction.ITEM_READ
    row = store.get_readable(conn, item_id, principal)
    if row is None:
        raise auditor.refuse(
            action,
            status_code=status.HTTP_404_NOT_FOUND,
            message=NOT_FOUND,
            target=_target(item_id),
            marking=store.marking_of(conn, item_id),
        )
    auditor.allowed(action, target=_target(item_id), marking=row.marking)
    return stac_item(row)


def _query(body: SearchIn) -> store.Query:
    start, end = parse_interval(body.datetime) if body.datetime else (None, None)
    cursor = Cursor.from_token(body.token) if body.token else None
    return store.Query(
        limit=body.limit,
        bbox=body.bbox,
        geometry=body.intersects.model_dump_json() if body.intersects else None,
        start=start,
        end=end,
        terms=body.q.split() if body.q else (),
        collections=body.collections,
        kinds=body.kinds,
        ids=body.ids,
        after=store.Position(cursor.created_at, cursor.id) if cursor else None,
    )


@router.post(SEARCH_PATH)
def search(
    body: SearchIn, principal: CurrentPrincipal, conn: Conn, auditor: Audit
) -> ItemCollection:
    """One page of readable items matching every filter, newest first. Follow the `next` link,
    merging its body into this request, for the following page."""
    rows = store.search(conn, principal, _query(body))
    page = rows[: body.limit]
    links: list[NextLink] = []
    if len(rows) > body.limit:
        last = page[-1]
        token = Cursor(created_at=last.created_at, id=last.id).token()
        links.append(NextLink(href=SEARCH_PATH, body=NextBody(token=token)))
    highest = max(page, key=lambda row: row.marking_rank, default=None)
    auditor.allowed(
        AuditAction.ITEM_SEARCH,
        marking=highest.marking if highest else None,
        detail={"ids": [str(row.id) for row in page]},
    )
    return ItemCollection(
        features=[stac_item(row) for row in page], links=links, numberReturned=len(page)
    )
