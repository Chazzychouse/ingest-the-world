"""SQL for catalog items. Typed rows out; no HTTP.

Only the pipeline creates items. An item's id is its upload's, and its original lives in
`raw/` under the same name the upload landed under (`uploads.store.blob_name`).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Metadata


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
    marking from the collection (ACC-3: the database refuses anything lower)."""
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
