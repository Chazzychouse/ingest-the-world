"""SQL for collections and grants. Typed rows out; no HTTP, no auditing.

Every read of collections filters through `authz.READABLE_IDS` so the access rule is applied
by the database in exactly one place (ACC-1).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection, errors
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from ingest_api import authz
from ingest_api.auth import Principal
from ingest_api.catalog.models import Access, CollectionIn, PrincipalType

# Hard caps on result sets (Power of 10, rule 3). Real pagination arrives with search in M1
# (SRCH-2); until then a catalog this large is a configuration problem, not a page to render.
MAX_COLLECTIONS = 1000
MAX_GRANTS = 1000


class AlreadyExists(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CollectionRow:
    id: str
    title: str
    description: str
    marking: str
    marking_label: str
    marking_color: str
    marking_rank: int
    cui_categories: list[str]
    dissemination: list[str]
    retention: dict[str, Any]  # jsonb, validated as `Retention` on the way in and out
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GrantKey:
    principal_type: PrincipalType
    principal: str


@dataclass(frozen=True, slots=True)
class GrantRow:
    principal_type: PrincipalType
    principal: str
    access: Access


_SELECT_READABLE = f"""
    SELECT c.id, c.title, c.description, c.marking, m.label AS marking_label,
           m.banner_color AS marking_color, m.rank AS marking_rank, c.cui_categories,
           c.dissemination, c.retention, c.created_by, c.created_at
    FROM catalog.collections c JOIN authz.markings m ON m.code = c.marking
    WHERE c.id IN ({authz.READABLE_IDS})
"""  # noqa: S608 - composed from constants only


def list_readable(conn: Connection[TupleRow], principal: Principal) -> list[CollectionRow]:
    """Collections `principal` may read, highest marking first."""
    with conn.cursor(row_factory=class_row(CollectionRow)) as cur:
        return cur.execute(
            _SELECT_READABLE + " ORDER BY m.rank DESC, c.title LIMIT %s",
            (*authz.principal_args(principal), MAX_COLLECTIONS),
        ).fetchall()


def get_readable(
    conn: Connection[TupleRow], collection_id: str, principal: Principal
) -> CollectionRow | None:
    with conn.cursor(row_factory=class_row(CollectionRow)) as cur:
        return cur.execute(
            _SELECT_READABLE + " AND c.id = %s", (*authz.principal_args(principal), collection_id)
        ).fetchone()


def marking_of(conn: Connection[TupleRow], collection_id: str) -> str | None:
    """The marking of a collection regardless of access. For audit records only."""
    row = conn.execute(
        "SELECT marking FROM catalog.collections WHERE id = %s", (collection_id,)
    ).fetchone()
    return None if row is None else row[0]


def insert(conn: Connection[TupleRow], collection: CollectionIn, *, created_by: str) -> None:
    """Inserts in the caller's transaction. Raises AlreadyExists on a duplicate id."""
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO catalog.collections
                    (id, title, description, marking, cui_categories, dissemination,
                     retention, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    collection.id,
                    collection.title,
                    collection.description,
                    collection.marking,
                    collection.cui_categories,
                    collection.dissemination,
                    Jsonb(collection.retention.model_dump()),
                    created_by,
                ),
            )
    except errors.UniqueViolation as exc:
        raise AlreadyExists(collection.id) from exc


def list_grants(conn: Connection[TupleRow], collection_id: str) -> list[GrantRow]:
    with conn.cursor(row_factory=class_row(GrantRow)) as cur:
        return cur.execute(
            """
            SELECT principal_type, principal, access FROM authz.collection_grants
            WHERE collection_id = %s ORDER BY principal_type, principal LIMIT %s
            """,
            (collection_id, MAX_GRANTS),
        ).fetchall()


def put_grant(
    conn: Connection[TupleRow], collection_id: str, key: GrantKey, access: Access
) -> None:
    conn.execute(
        """
        INSERT INTO authz.collection_grants (collection_id, principal_type, principal, access)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (collection_id, principal_type, principal)
            DO UPDATE SET access = excluded.access
        """,
        (collection_id, key.principal_type, key.principal, access),
    )


def delete_grant(conn: Connection[TupleRow], collection_id: str, key: GrantKey) -> int:
    """Removes a grant; returns how many rows went (0 or 1)."""
    return conn.execute(
        """
        DELETE FROM authz.collection_grants
        WHERE collection_id = %s AND principal_type = %s AND principal = %s
        """,
        (collection_id, key.principal_type, key.principal),
    ).rowcount
