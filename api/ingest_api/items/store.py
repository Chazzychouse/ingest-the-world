"""SQL for catalog items. Typed rows out; no HTTP.

Only the pipeline creates items. An item's id is its upload's, and its original lives in
`raw/` under the same name the upload landed under (`uploads.store.blob_name`).

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

from ingest_api import authz
from ingest_api.auth import Principal
from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Metadata, Status

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
    box = metadata.bbox
    conn.execute(
        """
        INSERT INTO catalog.items
            (id, collection_id, kind, media_type, filename, size_bytes, sha256, raw_blob,
             marking, tags, created_by, created_at,
             extraction, extracted_bytes, crs, footprint, properties)
        SELECT u.id, u.collection_id, %s, %s, u.filename, %s, %s, u.blob_name, c.marking,
               u.tags, u.created_by, %s,
               %s, %s, %s, ST_MakeEnvelope(%s, %s, %s, %s, 4326), %s
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
            upload_id,
        ),
    )


@dataclass(frozen=True, slots=True)
class ItemRow:
    id: UUID
    collection_id: str
    kind: str
    media_type: str
    filename: str
    size_bytes: int
    sha256: str
    marking: str
    marking_rank: int
    tags: list[str]
    created_at: datetime
    extraction: Status
    crs: str | None
    footprint: dict[str, Any] | None  # GeoJSON, from PostGIS
    bbox: list[float] | None  # west, south, east, north
    properties: dict[str, Any]  # jsonb, written by the pipeline from `Metadata.properties`


_SELECT_READABLE = sql.SQL(f"""
    SELECT i.id, i.collection_id, i.kind, i.media_type, i.filename, i.size_bytes, i.sha256,
           i.marking, m.rank AS marking_rank, i.tags, i.created_at, i.extraction, i.crs,
           ST_AsGeoJSON(i.footprint)::jsonb AS footprint,
           CASE WHEN i.footprint IS NOT NULL THEN ARRAY[
               ST_XMin(i.footprint), ST_YMin(i.footprint),
               ST_XMax(i.footprint), ST_YMax(i.footprint)
           ] END AS bbox,
           i.properties
    FROM catalog.items i JOIN authz.markings m ON m.code = i.marking
    WHERE (i.collection_id, i.marking) IN ({authz.READABLE_SCOPES})
""")  # noqa: S608 - composed from constants only


def get_readable(conn: Connection[TupleRow], item_id: UUID, principal: Principal) -> ItemRow | None:
    query = _SELECT_READABLE + sql.SQL(" AND i.id = %s")
    with conn.cursor(row_factory=class_row(ItemRow)) as cur:
        return cur.execute(query, (*authz.principal_args(principal), item_id)).fetchone()


def marking_of(conn: Connection[TupleRow], item_id: UUID) -> str | None:
    """The marking of an item regardless of access. For audit records only."""
    row = conn.execute("SELECT marking FROM catalog.items WHERE id = %s", (item_id,)).fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class Position:
    """Where a page ended in the search order: newest first, ties broken by id."""

    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class Query:
    """A search. Every filter given must hold (SRCH-1); None means no filter, and an empty
    tuple matches nothing."""

    limit: int
    bbox: tuple[float, float, float, float] | None = None  # west > east crosses 180°
    geometry: str | None = None  # GeoJSON, validated at the edge
    start: datetime | None = None
    end: datetime | None = None
    terms: Sequence[str] = field(default_factory=tuple[str, ...])
    collections: Sequence[str] | None = None
    kinds: Sequence[Kind] | None = None
    ids: Sequence[UUID] | None = None
    after: Position | None = None


def search(conn: Connection[TupleRow], principal: Principal, query: Query) -> list[ItemRow]:
    """Up to `query.limit + 1` readable matches after `query.after`, in search order; one more
    than asked for means there is a next page."""
    where, params = _filters(query)
    statement = sql.Composed(
        [
            _SELECT_READABLE,
            *(sql.SQL(" AND ") + clause for clause in where),
            sql.SQL(" ORDER BY i.created_at DESC, i.id DESC LIMIT %s"),
        ]
    )
    with conn.cursor(row_factory=class_row(ItemRow)) as cur:
        args = (*authz.principal_args(principal), *params, query.limit + 1)
        return cur.execute(statement, args).fetchall()


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
    if query.start is not None:
        clauses.append((sql.SQL("i.created_at >= %s"), (query.start,)))
    if query.end is not None:
        clauses.append((sql.SQL("i.created_at <= %s"), (query.end,)))
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
