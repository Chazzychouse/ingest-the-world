"""HTTP for items, their files and search: parse, ask the store, audit, render as STAC.

Every handler ends in exactly one `auditor.allowed(...)` or `raise auditor.refuse(...)`
(ACC-5). An item the caller can't read answers 404, as if missing (ACC-2), for its files as for
its metadata (ACC-9); a search simply never returns it (ACC-1). Only cataloged files are items,
so nothing quarantined or rejected can be served (ING-4).

Files never pass through the API: a request for one is answered with a read-only link to that
one blob, valid for minutes (ACC-8).
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response, status
from fastapi.responses import RedirectResponse

from ingest_api import paging
from ingest_api.audit import AuditAction, Auditor, Target
from ingest_api.auth import Principal
from ingest_api.deps import Audit, Conn, CurrentPrincipal, Rt, Runtime
from ingest_api.items import store
from ingest_api.items.metadata import CLOUD_OPTIMIZED
from ingest_api.items.models import (
    FILE_EXTENSION,
    ORIGINAL,
    PROJECTION_EXTENSION,
    Asset,
    AssetUrl,
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
from ingest_api.ports import Container

router = APIRouter(tags=["items"])

NOT_FOUND = "item not found"
ASSET_NOT_FOUND = "asset not found"
# The links are secrets while they last, and the answer changes with access: never cached.
NO_STORE = {"Cache-Control": "no-store"}
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


def _times(row: store.ItemRow) -> dict[str, dt.datetime | None]:
    """STAC's time fields: one `datetime`, or a null one and the range's ends."""
    start, end = row.captured_start, row.captured_end
    if start is None or end is None:
        return {"datetime": row.created_at}
    if start == end:
        return {"datetime": start}
    return {"datetime": None, "start_datetime": start, "end_datetime": end}


def _checksum(sha256: str) -> str:
    return SHA256_MULTIHASH + sha256


def _asset(
    href: str, media_type: str, title: str, roles: list[str], file: tuple[int, str]
) -> Asset:
    size, sha256 = file
    return Asset.model_validate(
        {
            "href": href,
            "type": media_type,
            "title": title,
            "roles": roles,
            "file:size": size,
            "file:checksum": _checksum(sha256),
        }
    )


def _assets(row: store.ItemRow, derived: Sequence[store.AssetRow]) -> dict[str, Asset]:
    """The original, under `original`, and each derivative under its role."""
    href = f"/items/{row.id}/assets"
    cog = ["cloud-optimized"] if row.properties.get(CLOUD_OPTIMIZED) is True else []
    original = _asset(
        f"{href}/{ORIGINAL}",
        row.media_type,
        row.filename,
        ["data", *cog],
        (row.size_bytes, row.sha256),
    )
    return {ORIGINAL: original} | {
        asset.role: _asset(
            f"{href}/{asset.role}",
            asset.media_type,
            asset.title,
            asset.stac_roles,
            (asset.size_bytes, asset.sha256),
        )
        for asset in derived
    }


def stac_item(row: store.ItemRow, derived: Sequence[store.AssetRow]) -> StacItem:
    code, wkt2 = _crs(row.crs)
    core = {
        **_times(row),
        "created": row.created_at,
        "title": row.filename,
        "keywords": row.tags,
        "ingest:kind": row.kind,
        "ingest:media_type": row.media_type,
        "ingest:marking": row.marking,
        "ingest:marking_rank": row.marking_rank,
        "ingest:extraction": row.extraction,
        "file:size": row.size_bytes,
        "file:checksum": _checksum(row.sha256),
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
        assets=_assets(row, derived),
    )


def _readable_or_404(
    conn: Conn, auditor: Auditor, action: AuditAction, item_id: UUID, principal: Principal
) -> store.ItemRow:
    row = store.get_readable(conn, item_id, principal)
    if row is not None:
        return row
    raise auditor.refuse(
        action,
        status_code=status.HTTP_404_NOT_FOUND,
        message=NOT_FOUND,
        target=_target(item_id),
        marking=store.marking_of(conn, item_id),
    )


@router.get("/items/{item_id}")
def get_item(item_id: UUID, principal: CurrentPrincipal, conn: Conn, auditor: Audit) -> StacItem:
    action = AuditAction.ITEM_READ
    row = _readable_or_404(conn, auditor, action, item_id, principal)
    auditor.allowed(action, target=_target(item_id), marking=row.marking)
    return stac_item(row, store.assets_of(conn, [row.id]))


# A role as a path segment: anything else names no asset, and answers 404 like a missing one.
Role = Annotated[str, Path(min_length=1, max_length=32)]


@dataclass(frozen=True, slots=True)
class _Blob:
    container: Container
    name: str
    media_type: str
    filename: str  # what a download saves it as
    recipe_version: int | None  # None for the original


def _blob(conn: Conn, row: store.ItemRow, role: str) -> _Blob | None:
    if role == ORIGINAL:
        return _Blob(Container.RAW, row.raw_blob, row.media_type, row.filename, None)
    asset = store.asset_of(conn, row.id, role)
    if asset is None:
        return None
    # `ortho.tif`'s thumbnail downloads as `ortho.thumbnail.png`.
    filename = f"{PurePath(row.filename).stem}.{role}{PurePath(asset.blob_name).suffix}"
    return _Blob(
        Container.DERIVED, asset.blob_name, asset.media_type, filename, asset.recipe_version
    )


def _signed(
    request: tuple[UUID, str], principal: Principal, conn: Conn, auditor: Auditor, runtime: Runtime
) -> AssetUrl:
    """A read URL for a readable item's asset, audited; or the audited 404."""
    item_id, role = request
    action = AuditAction.ASSET_READ
    row = _readable_or_404(conn, auditor, action, item_id, principal)
    blob = _blob(conn, row, role)
    if blob is None:
        raise auditor.refuse(
            action,
            status_code=status.HTTP_404_NOT_FOUND,
            message=ASSET_NOT_FOUND,
            target=_target(item_id),
            marking=row.marking,
        )
    expires_at = runtime.clock.now() + dt.timedelta(minutes=runtime.settings.read_url_minutes)
    url = runtime.signer.read_url(
        blob.container,
        blob.name,
        expires_at=expires_at,
        media_type=blob.media_type,
        filename=blob.filename,
    )
    auditor.allowed(
        action,
        target=_target(item_id),
        marking=row.marking,
        detail={"role": role, "recipe_version": blob.recipe_version},
    )
    return AssetUrl(url=url, expires_at=expires_at)


@router.get(
    "/items/{item_id}/assets/{role}",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such item, or no such asset"}},
)
def get_asset(
    item_id: UUID,
    role: Role,
    principal: CurrentPrincipal,
    conn: Conn,
    auditor: Audit,
    runtime: Rt,
) -> RedirectResponse:
    """Redirects to an asset's bytes (`original`, or a derivative's role): a read-only link to
    that one file, valid for at most 15 minutes."""
    signed = _signed((item_id, role), principal, conn, auditor, runtime)
    return RedirectResponse(
        signed.url, status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers=NO_STORE
    )


def _no_store(response: Response) -> None:
    response.headers.update(NO_STORE)


@router.get(
    "/items/{item_id}/assets/{role}/url",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such item, or no such asset"}},
    dependencies=[Depends(_no_store)],
)
def get_asset_url(
    item_id: UUID,
    role: Role,
    principal: CurrentPrincipal,
    conn: Conn,
    auditor: Audit,
    runtime: Rt,
) -> AssetUrl:
    """The link `GET /items/{id}/assets/{role}` redirects to, as JSON, for clients that can't
    read a redirect: a browser's `fetch` hides it, and an `<img>` can't send a token."""
    return _signed((item_id, role), principal, conn, auditor, runtime)


def _query(body: SearchIn) -> store.Query:
    start, end = parse_interval(body.datetime) if body.datetime else (None, None)
    created_start, created_end = parse_interval(body.created) if body.created else (None, None)
    return store.Query(
        limit=body.limit,
        bbox=body.bbox,
        geometry=body.intersects.model_dump_json() if body.intersects else None,
        start=start,
        end=end,
        created_start=created_start,
        created_end=created_end,
        terms=body.q.split() if body.q else (),
        collections=body.collections,
        kinds=body.kinds,
        ids=body.ids,
        after=paging.position(body.token) if body.token else None,
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
        token = paging.token(paging.Position(last.created_at, last.id))
        links.append(NextLink(href=SEARCH_PATH, body=NextBody(token=token)))
    highest = max(page, key=lambda row: row.marking_rank, default=None)
    auditor.allowed(
        AuditAction.ITEM_SEARCH,
        marking=highest.marking if highest else None,
        detail={"ids": [str(row.id) for row in page]},
    )
    derived: dict[UUID, list[store.AssetRow]] = {row.id: [] for row in page}
    for asset in store.assets_of(conn, list(derived)):
        derived[asset.item_id].append(asset)
    return ItemCollection(
        features=[stac_item(row, derived[row.id]) for row in page],
        links=links,
        numberReturned=len(page),
    )
