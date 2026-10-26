"""Live upload status, by example: a stream of the caller's own uploads, sent when it opens and
again as each changes, audited once (ACC-1, ACC-5). Streams here poll every few milliseconds and
last about a second."""

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ingest_api.app import create_app
from ingest_api.uploads import store
from tests.audit_log import AuditLog
from tests.conftest import app_settings
from tests.invariants import check
from tests.test_uploads import _start

COLLECTION = "bio-lab-readings"  # carol and alice may both upload here
CUI_COLLECTION = "range-ops-cui"  # carol may upload here too, being cleared
POLL_SECONDS, STREAM_SECONDS = 0.02, 1.0


@pytest.fixture(scope="module")
def quick(database_url, idp) -> Iterator[TestClient]:
    """The API, its streams quick and short."""
    settings = app_settings(database_url).model_copy(
        update={"events_poll_seconds": POLL_SECONDS, "events_stream_seconds": STREAM_SECONDS}
    )
    with TestClient(create_app(settings, idp.verifier())) as client:
        yield client


def _lines(chunks: Iterator[str]) -> Iterator[str]:
    """A stream's lines, ended by "\n" alone, as events end them: JSON may hold other line
    breaks (U+2028) unescaped, which `iter_lines` would split on."""
    pending = ""
    for chunk in chunks:
        *lines, pending = (pending + chunk).split("\n")
        yield from lines


def _events(chunks: Iterator[str]) -> Iterator[dict]:
    """The `upload` events in a stream, as they arrive."""
    name = None
    for line in _lines(chunks):
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: ") and name == "upload":
            yield json.loads(line.removeprefix("data: "))


@pytest.mark.invariant("ACC-1", "ACC-5")
def test_a_stream_opens_with_the_callers_own_uploads_and_is_audited_once(quick, auth, db):
    # Older than the rest, and marked higher: what the audit event must carry.
    def started(user: str, collection: str) -> str:
        return _start(quick, auth, user, collection).json()["upload"]["id"]

    mine = {started("carol", CUI_COLLECTION), started("carol", COLLECTION)}
    mine.add(started("carol", COLLECTION))
    theirs = started("alice", COLLECTION)
    audit = AuditLog(db)
    audit.mark()

    with quick.stream("GET", "/uploads/events", headers=auth("carol")) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        sent = list(_events(response.iter_text()))

    ids = [upload["id"] for upload in sent]
    check("ACC-1", theirs not in ids, "carol's stream showed alice's upload")
    check("ACC-1", {u["created_by"] for u in sent} == {"sub-carol"}, f"{sent}")
    assert mine <= set(ids)
    assert len(ids) == len(set(ids)), "an unchanged upload was sent twice"
    watches = [e for e in audit.since() if e.action == "upload.watch"]
    check("ACC-5", len(watches) == 1, f"one stream, {len(watches)} audit events")
    # The same uploads as the list's first page of 100, so the same highest marking.
    assert quick.get("/uploads?limit=100", headers=auth("carol")).status_code == 200
    listed = audit.last(action="upload.list", actor_name="carol")
    watch = watches[0]
    check(
        "ACC-5",
        watch.outcome == "allowed" and watch.marking == listed.marking == "CUI",
        f"{watch} vs {listed}",
    )


def test_a_stream_sends_an_upload_again_when_it_changes(quick, auth, db):
    upload_id = _start(quick, auth, "carol", COLLECTION).json()["upload"]["id"]

    with quick.stream("GET", "/uploads/events", headers=auth("carol")) as response:
        seen = []
        for upload in _events(response.iter_text()):
            if upload["id"] != upload_id:
                continue
            seen.append((upload["status"], upload["stage"]))
            if len(seen) == 1:  # the snapshot: now the pipeline decides
                store.close(db, upload_id, "quarantined")
            else:
                break

    assert seen == [("pending", "awaiting-bytes"), ("quarantined", None)]


def test_a_stream_needs_a_signed_in_caller(quick):
    assert quick.get("/uploads/events").status_code == 401
