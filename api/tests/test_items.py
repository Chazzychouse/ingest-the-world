"""The database's own guards on items and jobs: the last line behind the pipeline's logic."""

import re
import uuid
from datetime import UTC, datetime
from typing import get_args

import psycopg
import pytest

from ingest_api.items import metadata
from ingest_api.items.kinds import Kind
from ingest_api.uploads.models import UploadStatus
from ingest_api.worker.jobs import Stage


def test_the_database_knows_exactly_the_kinds_the_code_does(db):
    rows = db.execute("SELECT code FROM catalog.kinds").fetchall()

    assert {code for (code,) in rows} == set(Kind)


def _allowed(db, constraint: str) -> set[str]:
    """The values a `CHECK (column IN (...))` constraint admits."""
    [(definition,)] = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s", (constraint,)
    ).fetchall()
    return set(re.findall(r"'([^']+)'::text", definition))


@pytest.mark.parametrize(
    ("constraint", "values"),
    [
        ("uploads_status_check", UploadStatus),
        ("jobs_stage_check", Stage),
        ("items_extraction_check", metadata.Status),
    ],
)
def test_the_database_admits_exactly_the_states_the_code_has(db, constraint, values):
    assert _allowed(db, constraint) == set(get_args(values))


def _upload(db, collection: str) -> uuid.UUID:
    upload_id = uuid.uuid4()
    now = datetime.now(UTC)
    db.execute(
        """
        INSERT INTO ingest.uploads (id, collection_id, filename, size_bytes, blob_name, created_by,
                                    created_at, url_expires_at)
        VALUES (%s, %s, 'f.bin', 1, %s, 'sub-test', %s, %s + interval '1 hour')
        """,
        (upload_id, collection, f"{collection}/{upload_id.hex}/f.bin", now, now),
    )
    return upload_id


@pytest.mark.invariant("ACC-3")
def test_an_item_cannot_be_marked_below_its_collection(db):
    upload_id = _upload(db, "range-ops-cui")

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            """
            INSERT INTO catalog.items (id, collection_id, kind, media_type, filename, size_bytes,
                                       sha256, raw_blob, marking, created_by, created_at)
            VALUES (%s, 'range-ops-cui', 'generic', 'application/octet-stream', 'f.bin', 1,
                    repeat('0', 64), %s, 'UNCLASSIFIED', 'sub-test', now())
            """,
            (upload_id, upload_id.hex),
        )


@pytest.mark.invariant("ING-2")
def test_a_job_outcome_cannot_change(db):
    name = f"t/{uuid.uuid4().hex}"
    db.execute(
        """
        INSERT INTO ingest.jobs (blob_name, etag, stage, reason, created_at, updated_at)
        VALUES (%s, 'e1', 'rejected', 'no-upload', now(), now())
        """,
        (name,),
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE ingest.jobs SET stage = 'cataloged', reason = NULL WHERE blob_name = %s",
            (name,),
        )


def _insert_item(db, upload_id: uuid.UUID, *, sha256: str, exempt: bool = False) -> None:
    db.execute(
        """
        INSERT INTO catalog.items (id, collection_id, kind, media_type, filename, size_bytes,
                                   sha256, raw_blob, marking, created_by, created_at,
                                   extraction, extracted_bytes, predates_dedup)
        VALUES (%s, 'site-survey-alpha', 'generic', 'application/octet-stream', 'f.bin', 1,
                %s, %s, 'UNCLASSIFIED', 'sub-test', now(), 'unsupported', 0, %s)
        """,
        (upload_id, sha256, upload_id.hex, exempt),
    )


@pytest.mark.invariant("ING-9")
def test_two_items_in_a_collection_cannot_share_a_checksum(db):
    sha256 = uuid.uuid4().hex * 2
    _insert_item(db, _upload(db, "site-survey-alpha"), sha256=sha256)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_item(db, _upload(db, "site-survey-alpha"), sha256=sha256)


@pytest.mark.invariant("ING-9")
def test_no_new_item_can_claim_the_dedup_exemption(db):
    sha256 = uuid.uuid4().hex * 2
    _insert_item(db, _upload(db, "site-survey-alpha"), sha256=sha256)

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_item(db, _upload(db, "site-survey-alpha"), sha256=sha256, exempt=True)
