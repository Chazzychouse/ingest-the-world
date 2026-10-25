"""Upload sessions: who may start one, who sees it, and what the signed URL allows."""

import uuid
from datetime import UTC, datetime

import pytest

from ingest_api.collections.routes import NOT_FOUND as COLLECTION_NOT_FOUND
from ingest_api.ports import Container
from ingest_api.uploads.routes import NO_WRITE_ACCESS
from ingest_api.uploads.routes import NOT_FOUND as UPLOAD_NOT_FOUND
from tests.audit_log import AuditLog, Event
from tests.signed_urls import check_upload_url

GHOST_UPLOAD = "00000000-0000-4000-8000-000000000000"


def _start(client, auth, user: str, collection_id: str, filename: str = "survey 01.tif"):
    body = {"collection_id": collection_id, "filename": filename, "size_bytes": 1024}
    return client.post("/uploads", json=body, headers=auth(user))


def _blob_name(db, upload_id: str) -> str:
    row = db.execute("SELECT blob_name FROM ingest.uploads WHERE id = %s", (upload_id,)).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.invariant("ACC-7", "ACC-8", "ACC-5")
def test_contributor_with_a_write_grant_gets_a_url_for_one_blob(client, auth, db):
    response = _start(client, auth, "carol", "site-survey-alpha")

    assert response.status_code == 201
    session = response.json()
    upload = session["upload"]
    assert (upload["status"], upload["marking"], upload["created_by"]) == (
        "pending",
        "UNCLASSIFIED",
        "sub-carol",
    )
    check_upload_url(
        session["upload_url"],
        container=Container.LANDING,
        blob_name=_blob_name(db, upload["id"]),
        issued_by=datetime.now(UTC),
    )
    assert AuditLog(db).last(target_id=upload["id"]) == Event(
        action="upload.create",
        outcome="allowed",
        marking="UNCLASSIFIED",
        actor_name="carol",
        target_id=upload["id"],
    )


def test_two_uploads_of_the_same_file_land_in_different_blobs(client, auth, db):
    first, second = (_start(client, auth, "carol", "bio-lab-readings").json() for _ in range(2))

    names = {_blob_name(db, s["upload"]["id"]) for s in (first, second)}
    assert len(names) == 2


@pytest.mark.invariant("ACC-7", "ACC-5")
def test_a_read_grant_does_not_allow_uploading(client, auth, db):
    response = _start(client, auth, "bob", "site-survey-alpha")

    assert (response.status_code, response.json()) == (403, {"detail": NO_WRITE_ACCESS})
    event = AuditLog(db).last(action="upload.create", actor_name="bob")
    assert (event.outcome, event.marking, event.target_id) == (
        "denied",
        "UNCLASSIFIED",
        "site-survey-alpha",
    )


@pytest.mark.invariant("ACC-2")
def test_uploading_into_an_unreadable_collection_looks_like_a_missing_one(client, auth):
    hidden = _start(client, auth, "bob", "range-ops-cui")
    missing = _start(client, auth, "bob", "no-such-collection")

    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())
    assert hidden.json() == {"detail": COLLECTION_NOT_FOUND}


@pytest.mark.invariant("ACC-1", "ACC-2", "ACC-5")
def test_an_upload_is_visible_exactly_to_readers_of_its_collection(client, auth, db):
    upload_id = _start(client, auth, "carol", "range-ops-cui").json()["upload"]["id"]

    assert client.get(f"/uploads/{upload_id}", headers=auth("alice")).json()["marking"] == "CUI"
    hidden = client.get(f"/uploads/{upload_id}", headers=auth("bob"))
    missing = client.get(f"/uploads/{GHOST_UPLOAD}", headers=auth("bob"))
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())
    assert hidden.json() == {"detail": UPLOAD_NOT_FOUND}
    event = AuditLog(db).last(target_id=upload_id, actor_name="bob")
    assert (event.action, event.outcome, event.marking) == ("upload.read", "denied", "CUI")


@pytest.mark.invariant("ACC-7", "ACC-8")
def test_only_the_uploader_renews_the_url(client, auth, db):
    upload_id = _start(client, auth, "carol", "site-survey-alpha").json()["upload"]["id"]

    assert client.post(f"/uploads/{upload_id}/url", headers=auth("alice")).status_code == 403
    renewed = client.post(f"/uploads/{upload_id}/url", headers=auth("carol"))
    assert renewed.status_code == 200
    check_upload_url(
        renewed.json()["upload_url"],
        container=Container.LANDING,
        blob_name=_blob_name(db, upload_id),
        issued_by=datetime.now(UTC),
    )


@pytest.mark.invariant("ACC-5")
def test_a_finished_upload_gets_no_new_url(client, auth, db):
    upload_id = _start(client, auth, "carol", "site-survey-alpha").json()["upload"]["id"]
    db.execute("UPDATE ingest.uploads SET status = 'cataloged' WHERE id = %s", (upload_id,))

    response = client.post(f"/uploads/{upload_id}/url", headers=auth("carol"))

    assert (response.status_code, response.json()) == (409, {"detail": "upload is cataloged"})
    assert AuditLog(db).last(target_id=upload_id).outcome == "denied"


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "a/b", "a\\b", "ends-with-dot.", "ends-with-space ", "nul\x00", "x" * 256],
)
def test_filenames_that_are_not_one_safe_path_segment_are_rejected(client, auth, filename):
    assert _start(client, auth, "carol", "site-survey-alpha", filename).status_code == 422


@pytest.mark.parametrize(
    "field",
    [
        {"size_bytes": 0},
        {"size_bytes": 2**40 + 1},
        {"sha256": "ABC"},
        {"content_type": "not a media type"},
        {"tags": ["t"] * 33},
    ],
)
def test_out_of_range_fields_are_rejected(client, auth, field):
    body = {"collection_id": "site-survey-alpha", "filename": "f.bin", "size_bytes": 1, **field}

    assert client.post("/uploads", json=body, headers=auth("carol")).status_code == 422


def test_upload_ids_are_uuids(client, auth):
    assert client.get("/uploads/not-a-uuid", headers=auth("carol")).status_code == 422
    assert uuid.UUID(_start(client, auth, "carol", "site-survey-alpha").json()["upload"]["id"])
