"""SQL for catalog items. Typed rows out; no HTTP.

Only the pipeline creates items. An item's id is its upload's, and its original lives in
`raw/` under the same name the upload landed under (`uploads.store.blob_name`).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow

from ingest_api.items.kinds import Kind


@dataclass(frozen=True, slots=True)
class Contents:
    """What the pipeline found in a promoted original."""

    kind: Kind
    media_type: str
    sha256: str
    size_bytes: int


def insert_from_upload(
    conn: Connection[TupleRow], upload_id: UUID, contents: Contents, *, created_at: datetime
) -> None:
    """Catalogs an upload. Its collection, name, tags and creator come from the upload, and its
    marking from the collection (ACC-3: the database refuses anything lower)."""
    conn.execute(
        """
        INSERT INTO catalog.items
            (id, collection_id, kind, media_type, filename, size_bytes, sha256, raw_blob,
             marking, tags, created_by, created_at)
        SELECT u.id, u.collection_id, %s, %s, u.filename, %s, %s, u.blob_name, c.marking,
               u.tags, u.created_by, %s
        FROM ingest.uploads u JOIN catalog.collections c ON c.id = u.collection_id
        WHERE u.id = %s
        """,
        (
            contents.kind,
            contents.media_type,
            contents.size_bytes,
            contents.sha256,
            created_at,
            upload_id,
        ),
    )
