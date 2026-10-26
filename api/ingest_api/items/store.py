"""SQL for catalog items. Typed rows out; no HTTP.

Only the pipeline creates items, and only the derive stage adds their assets. An item's id is its
upload's, and its original lives in `raw/` under the same name the upload landed under
(`uploads.store.blob_name`).

Every read filters through `authz.READABLE_SCOPES`, so the database decides which items a
principal may see, for a single item and a search alike (ACC-1).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection, errors, sql
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from ingest_api import authz
from ingest_api.auth import Principal
from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Metadata, Status
from ingest_api.items.models import Derivatives, DeriveReason
from ingest_api.paging import Position

# The index that keeps one item per content per collection (migration 0007, ING-9).
ONE_PER_CONTENT = "items_one_per_content"


class Duplicate(Exception):
    """The collection already has an item with these bytes (ING-9)."""


@dataclass(frozen=True, slots=True)
class Contents:
    """What the pipeline found in a promoted original."""

    kind: Kind
    media_type: str
    sha256: str
    size_bytes: int
    metadata: Metadata


def insert_from_upload(
    conn: Connection[TupleRow], upload_id: UUID, contents: Contents, *, created_at: datetime
) -> None:
    """Catalogs an upload. Its collection, name, tags and creator come from the upload, and its
    marking from the collection (ACC-3: the database refuses anything lower). Raises Duplicate,
    with the transaction rolled back, if the collection already holds these bytes (ING-9)."""
    try:
        _insert(conn, upload_id, contents, created_at=created_at)
    except errors.UniqueViolation as exc:
        if exc.diag.constraint_name != ONE_PER_CONTENT:
            raise
        conn.rollback()
        raise Duplicate(contents.sha256) from exc


def _insert(
    conn: Connection[TupleRow], upload_id: UUID, contents: Contents, *, created_at: datetime
) -> None:
    metadata = contents.metadata
    box, when = metadata.bbox, metadata.captured
    conn.execute(
        """
        INSERT INTO catalog.items
            (id, collection_id, kind, media_type, filename, size_bytes, sha256, raw_blob,
             marking, tags, created_by, created_at,
             extraction, extracted_bytes, crs, footprint, properties, captured)
        SELECT u.id, u.collection_id, %s, %s, u.filename, %s, %s, u.blob_name, c.marking,
               u.tags, u.created_by, %s,
               %s, %s, %s, ST_MakeEnvelope(%s, %s, %s, %s, 4326), %s, %s
        FROM ingest.uploads u JOIN catalog.collections c ON c.id = u.collection_id
        WHERE u.id = %s
        """,
        (
            contents.kind,
            contents.media_type,
            contents.size_bytes,
            contents.sha256,
            created_at,
            metadata.status,
            metadata.bytes_read,
            metadata.crs,
            *((box.west, box.south, box.east, box.north) if box else (None,) * 4),
            Jsonb(dict(metadata.properties)),
            Range(when.start, when.end, "[]") if when else None,
            upload_id,
        ),
    )


@dataclass(frozen=True, slots=True)
class Original:
    """What the derive stage needs to know about an item's original in `raw/`."""

    id: UUID
    collection_id: str
    kind: Kind
    media_type: str
    raw_blob: str
    size_bytes: int
    sha256: str
    extraction: Status
    crs: str | None
    properties: dict[str, Any]


def original_of(conn: Connection[TupleRow], item_id: UUID) -> Original | None:
    """The item's original regardless of access, for the derive stage; None if there is no such
    item, as for anything quarantined or rejected (ING-11)."""
    with conn.cursor(row_factory=class_row(Original)) as cur:
        return cur.execute(
            "SELECT id, collection_id, kind, media_type, raw_blob, size_bytes, sha256, "
            "extraction, crs, properties FROM catalog.items WHERE id = %s",
            (item_id,),
        ).fetchone()


@dataclass(frozen=True, slots=True)
class NewAsset:
    item_id: UUID
    role: str
    recipe_version: int
    media_type: str
    blob_name: str  # in `derived/`
    size_bytes: int
    sha256: str
    lineage: dict[str, Any]  # STAC `processing:` fields
    title: str
    stac_roles: Sequence[str]


def record_asset(conn: Connection[TupleRow], asset: NewAsset, *, created_at: datetime) -> None:
    """Records a derivative whose blob is already written. Recording it again is a no-op: there
    is one asset per (item, role, recipe version) (ING-12)."""
    conn.execute(
        """
        INSERT INTO catalog.assets (item_id, role, recipe_version, media_type, blob_name,
                                    size_bytes, sha256, lineage, title, stac_roles, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (item_id, role, recipe_version) DO NOTHING
        """,
        (
            asset.item_id,
            asset.role,
            asset.recipe_version,
            asset.media_type,
            asset.blob_name,
            asset.size_bytes,
            asset.sha256,
            Jsonb(asset.lineage),
            asset.title,
            list(asset.stac_roles),
            created_at,
        ),
    )


@dataclass(frozen=True, slots=True)
class AssetRow:
    """A derivative of an item, at its newest recipe version."""

    item_id: UUID
    role: str
    recipe_version: int
    media_type: str
    blob_name: str  # in `derived/`
    size_bytes: int
    sha256: str
    title: str
    stac_roles: list[str]


_SELECT_NEWEST_ASSETS = """
    SELECT DISTINCT ON (item_id, role)
           item_id, role, recipe_version, media_type, blob_name, size_bytes, sha256, title,
           stac_roles
    FROM catalog.assets
"""
_NEWEST_FIRST = " ORDER BY item_id, role, recipe_version DESC"


def assets_of(conn: Connection[TupleRow], item_ids: Sequence[UUID]) -> list[AssetRow]:
    """The newest derivative of each role for these items, regardless of access: the caller has
    already read the items through `authz.READABLE_SCOPES`."""
    query = _SELECT_NEWEST_ASSETS + " WHERE item_id = ANY(%s)" + _NEWEST_FIRST
    with conn.cursor(row_factory=class_row(AssetRow)) as cur:
        return cur.execute(query, (list(item_ids),)).fetchall()


def asset_of(conn: Connection[TupleRow], item_id: UUID, role: str) -> AssetRow | None:
    """The item's newest derivative in `role`, regardless of access, like `assets_of`."""
    query = _SELECT_NEWEST_ASSETS + " WHERE item_id = %s AND role = %s" + _NEWEST_FIRST
    with conn.cursor(row_factory=class_row(AssetRow)) as cur:
        return cur.execute(query, (item_id, role)).fetchone()


@dataclass(frozen=True, slots=True)
class ItemRow:
    id: UUID
    collection_id: str
    kind: str
    media_type: str
    filename: str
    size_bytes: int
    sha256: str
    raw_blob: str  # the original, in `raw/`
    marking: str
    marking_rank: int
    tags: list[str]
    created_at: datetime
    captured_start: datetime | None  # when it was captured, both ends inclusive, if known
    captured_end: datetime | None
    extraction: Status
    crs: str | None
    footprint: dict[str, Any] | None  # GeoJSON, from PostGIS
    bbox: list[float] | None  # west, south, east, north
    properties: dict[str, Any]  # jsonb, written by the pipeline from `Metadata.properties`
    derivatives: Derivatives
    derivative_failures: dict[
        str, DeriveReason
    ]  # role -> why it failed, for each derivative it lacks


# Items the principal may read, as `i`, with their marking as `m`: every read starts here (ACC-1).
_FROM_READABLE = sql.SQL(f"""
    FROM catalog.items i JOIN authz.markings m ON m.code = i.marking
    WHERE (i.collection_id, i.marking) IN ({authz.READABLE_SCOPES})
""")

_COLUMNS = sql.SQL("""
    SELECT i.id, i.collection_id, i.kind, i.media_type, i.filename, i.size_bytes, i.sha256,
           i.raw_blob, i.marking, m.rank AS marking_rank, i.tags, i.created_at,
           lower(i.captured) AS captured_start, upper(i.captured) AS captured_end,
           i.extraction, i.crs,
           ST_AsGeoJSON(i.footprint)::jsonb AS footprint,
           CASE WHEN i.footprint IS NOT NULL THEN ARRAY[
               ST_XMin(i.footprint), ST_YMin(i.footprint),
               ST_XMax(i.footprint), ST_YMax(i.footprint)
           ] END AS bbox,
           i.properties, ingest.derivatives(i.id) AS derivatives,
           ingest.derivative_failures(i.id) AS derivative_failures
""")
_SELECT_READABLE = _COLUMNS + _FROM_READABLE


def get_readable(conn: Connection[TupleRow], item_id: UUID, principal: Principal) -> ItemRow | None:
    query = _SELECT_READABLE + sql.SQL(" AND i.id = %s")
    with conn.cursor(row_factory=class_row(ItemRow)) as cur:
        return cur.execute(query, (*authz.principal_args(principal), item_id)).fetchone()


def marking_of(conn: Connection[TupleRow], item_id: UUID) -> str | None:
    """The marking of an item regardless of access. For audit records only."""
    row = conn.execute("SELECT marking FROM catalog.items WHERE id = %s", (item_id,)).fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class Query:
    """A search. Every filter given must hold (SRCH-1); None means no filter, and an empty
    tuple matches nothing."""

    limit: int
    bbox: tuple[float, float, float, float] | None = None  # west > east crosses 180°
    geometry: str | None = None  # GeoJSON, validated at the edge
    start: datetime | None = None  # `datetime`: capture time, or cataloging time if unknown
    end: datetime | None = None
    created_start: datetime | None = None  # `created`: cataloging time
    created_end: datetime | None = None
    terms: Sequence[str] = field(default_factory=tuple[str, ...])
    collections: Sequence[str] | None = None
    kinds: Sequence[Kind] | None = None
    ids: Sequence[UUID] | None = None
    after: Position | None = None


def search(conn: Connection[TupleRow], principal: Principal, query: Query) -> list[ItemRow]:
    """Up to `query.limit + 1` readable matches after `query.after`, in search order; one more
    than asked for means there is a next page."""
    matching, params = _matching(principal, query)
    statement = sql.Composed(
        [_COLUMNS, matching, sql.SQL(" ORDER BY i.created_at DESC, i.id DESC LIMIT %s")]
    )
    with conn.cursor(row_factory=class_row(ItemRow)) as cur:
        return cur.execute(statement, (*params, query.limit + 1)).fetchall()


def _matching(principal: Principal, query: Query) -> tuple[sql.Composed, list[object]]:
    """`FROM ... WHERE ...` for the readable items matching every filter, and its parameters."""
    where, params = _filters(query)
    clauses = sql.Composed([_FROM_READABLE, *(sql.SQL(" AND ") + clause for clause in where)])
    return clauses, [*authz.principal_args(principal), *params]


def highest_marking(conn: Connection[TupleRow], principal: Principal, query: Query) -> str | None:
    """The highest marking among the readable items matching `query`, if any match."""
    matching, params = _matching(principal, query)
    statement = sql.SQL("SELECT i.marking ") + matching + sql.SQL(" ORDER BY m.rank DESC LIMIT 1")
    row = conn.execute(statement, params).fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class Tile:
    """A web map tile: zoom, column and row in the XYZ scheme over Web Mercator."""

    z: int
    x: int
    y: int


# The tile's extent, in its own units, and the margin of it drawn past each edge.
MVT_EXTENT = 4096
MVT_BUFFER = 64
FOOTPRINT_LAYER = "footprints"
# Web Mercator's latitude limit: footprints are clipped to it before they're projected.
MERCATOR_LATITUDE = 85.0511287798066

# A footprint drawn as its shape where that is at least a pixel, and elsewhere as a point on it,
# so a small item is still seen and picked at low zoom.
_FOOTPRINT_TILE = (
    sql.SQL("""
    WITH tile AS (SELECT ST_TileEnvelope(%s, %s, %s) AS envelope)
    SELECT ST_AsMVT(f, %s, %s, 'geom') FROM (
        SELECT id, collection, kind, marking, title, COALESCE(
                   ST_AsMVTGeom(shape, (SELECT envelope FROM tile), %s, %s, true),
                   ST_AsMVTGeom(ST_PointOnSurface(shape), (SELECT envelope FROM tile), %s, %s, true)
               ) AS geom
        FROM (
            SELECT i.id::text AS id, i.collection_id AS collection, i.kind, i.marking,
                   i.filename AS title,
                   ST_Transform(
                       ST_Intersection(i.footprint, ST_MakeEnvelope(-180, -%s, 180, %s, 4326)),
                       3857
                   ) AS shape
    """),
    sql.SQL("""
            AND i.footprint && (SELECT ST_Transform(envelope, 4326) FROM tile)
            ORDER BY i.created_at DESC, i.id DESC LIMIT %s
        ) s
    ) f WHERE f.geom IS NOT NULL
    """),
)


def footprint_tile(
    conn: Connection[TupleRow], principal: Principal, query: Query, tile: Tile
) -> bytes:
    """A Mapbox Vector Tile of the footprints of up to `query.limit` readable items matching
    `query` in `tile`, newest first: the search's own query, drawn instead of listed."""
    matching, params = _matching(principal, query)
    head, tail = _FOOTPRINT_TILE
    shape = (tile.z, tile.x, tile.y, FOOTPRINT_LAYER, MVT_EXTENT)
    fit = (MVT_EXTENT, MVT_BUFFER) * 2
    clip = (MERCATOR_LATITUDE, MERCATOR_LATITUDE)
    statement = sql.Composed([head, matching, tail])
    row = conn.execute(statement, (*shape, *fit, *clip, *params, query.limit)).fetchone()
    return b"" if row is None or row[0] is None else bytes(row[0])


_ENVELOPE = sql.SQL("ST_Intersects(i.footprint, ST_MakeEnvelope(%s, %s, %s, %s, 4326))")
_GEOMETRY = sql.SQL(
    "ST_Intersects(i.footprint, ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))"
)


type Clause = tuple[sql.Composable, tuple[object, ...]]


def _filters(query: Query) -> tuple[list[sql.Composable], list[object]]:
    """The WHERE clauses for a query's filters, and their parameters in order."""
    clauses = [*_where_and_when(query), *_facets(query)]
    if query.after is not None:
        after = query.after
        clauses.append((sql.SQL("(i.created_at, i.id) < (%s, %s)"), (after.created_at, after.id)))
    return [clause for clause, _ in clauses], [value for _, values in clauses for value in values]


def _where_and_when(query: Query) -> list[Clause]:
    clauses: list[Clause] = []
    if query.bbox is not None:
        envelopes = _envelopes(query.bbox)
        either = sql.SQL(" OR ").join([_ENVELOPE] * len(envelopes))
        values = tuple(value for envelope in envelopes for value in envelope)
        clauses.append((sql.Composed([sql.SQL("("), either, sql.SQL(")")]), values))
    if query.geometry is not None:
        clauses.append((_GEOMETRY, (query.geometry,)))
    if query.start is not None or query.end is not None:
        # Null bounds are open, so an interval with one open end overlaps everything that way.
        overlaps = sql.SQL("i.datetime_range && tstzrange(%s, %s, '[]')")
        clauses.append((overlaps, (query.start, query.end)))
    if query.created_start is not None:
        clauses.append((sql.SQL("i.created_at >= %s"), (query.created_start,)))
    if query.created_end is not None:
        clauses.append((sql.SQL("i.created_at <= %s"), (query.created_end,)))
    return clauses


def _facets(query: Query) -> list[Clause]:
    clauses: list[Clause] = [
        (sql.SQL("i.search_text LIKE lower(%s)"), (_containing(term),)) for term in query.terms
    ]
    if query.collections is not None:
        clauses.append((sql.SQL("i.collection_id = ANY(%s)"), (list(query.collections),)))
    if query.kinds is not None:
        clauses.append((sql.SQL("i.kind = ANY(%s)"), ([str(kind) for kind in query.kinds],)))
    if query.ids is not None:
        clauses.append((sql.SQL("i.id = ANY(%s)"), (list(query.ids),)))
    return clauses


def _envelopes(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    """A bbox as envelopes PostGIS can intersect: two when it crosses the antimeridian."""
    west, south, east, north = bbox
    if west <= east:
        return [bbox]
    return [(west, south, 180.0, north), (-180.0, south, east, north)]


def _containing(term: str) -> str:
    """A LIKE pattern matching `term` anywhere, with its own wildcards taken literally."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
