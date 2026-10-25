"""Cataloging items straight into the database, through the stores the pipeline writes with, for
tests about reading and searching them."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from ingest_api.items import store as items
from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox, Metadata
from ingest_api.uploads import store as uploads
from ingest_api.uploads.models import UploadIn


@dataclass(frozen=True, slots=True)
class NewItem:
    collection: str
    filename: str
    created_at: datetime
    tags: tuple[str, ...] = ()
    kind: Kind = Kind.GENERIC
    bbox: Bbox | None = None
    raise_to: str | None = None  # a marking above its collection's
    # Its id, which breaks ties in search order: random unless a simulation draws it, so that
    # where a new item sorts against a page cursor replays exactly.
    id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Cataloged:
    """An item a test cataloged, and the marking it ended up with."""

    id: uuid.UUID
    marking: str
    item: NewItem


def catalog(db: psycopg.Connection, new: NewItem) -> Cataloged:
    """Catalogs an item as the pipeline would, from an upload of its own."""
    upload_id = new.id or uuid.uuid4()
    upload = UploadIn(
        collection_id=new.collection, filename=new.filename, size_bytes=1, tags=list(new.tags)
    )
    uploads.insert(
        db,
        upload_id,
        upload,
        created_by="sub-test",
        created_at=new.created_at,
        url_expires_at=new.created_at + timedelta(hours=1),
    )
    status = "extracted" if new.bbox else "unsupported"
    metadata = Metadata(status, 0, bbox=new.bbox)
    sha256 = uuid.uuid4().hex * 2
    contents = items.Contents(new.kind, "application/octet-stream", sha256, 1, metadata)
    items.insert_from_upload(db, upload_id, contents, created_at=new.created_at)
    uploads.close(db, upload_id, "cataloged")
    if new.raise_to is not None:
        db.execute("UPDATE catalog.items SET marking = %s WHERE id = %s", (new.raise_to, upload_id))
    row = db.execute("SELECT marking FROM catalog.items WHERE id = %s", (upload_id,)).fetchone()
    assert row is not None
    return Cataloged(upload_id, row[0], new)
