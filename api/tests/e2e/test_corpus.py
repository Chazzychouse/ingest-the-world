"""Every sample in `samples/manifest.json`, uploaded through the running stack, ends as the
manifest says: the right outcome, and for cataloged files the right kind and metadata.

    make samples && make e2e       # INGEST_E2E_LARGE=1 adds the multi-GB samples

Each run makes its own collections (as alice), so reruns never meet an earlier run's files,
and uploads as carol, a Contributor granted write on them. The outcome is read through the API
as the web app will; the item's metadata from the stack's database, until items have a route of
their own (slice 5). Expectations live only in the manifest.
"""

import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import psycopg
import pytest

from tests.e2e.stack import API_URL, DATABASE_URL, TIMEOUT, Api, sign_in, upload_blocks
from tests.invariants import check
from tools import samples

pytestmark = pytest.mark.e2e

CORPUS = samples.selected(samples.load(), large=bool(os.environ.get("INGEST_E2E_LARGE")))
# Polling for an outcome: a floor for small files, plus time for the worker to copy and hash.
POLL_SECONDS = 1.0
BASE_WAIT_SECONDS = 60.0
WAIT_BYTES_PER_SECOND = 20 * 1024 * 1024
WRONG_SHA256 = "0" * 64


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API_URL, timeout=TIMEOUT) as client:
        yield client


@pytest.fixture(scope="module")
def collection(http: httpx.Client) -> Callable[[str], str]:
    """The run's collection for a label, made on first use, writable by Contributors."""
    admin = Api(http, sign_in("alice"))
    run = uuid.uuid4().hex[:8]
    made: dict[str, str] = {}

    def get(label: str) -> str:
        if label not in made:
            collection_id = f"e2e-{run}-{label}"
            admin.post("/collections", {"id": collection_id, "title": f"E2E {run} {label}"})
            admin.put(f"/collections/{collection_id}/grants/role/Contributor", {"access": "write"})
            made[label] = collection_id
        return made[label]

    return get


@pytest.fixture(scope="module")
def stack_db() -> Iterator[psycopg.Connection]:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        yield conn


def _declared(sample: samples.Sample, upload: samples.Upload) -> str | None:
    match upload.declared_sha256:
        case "right":
            return sample.sha256
        case "wrong":
            return WRONG_SHA256
        case "none":
            return None


def _await_outcome(api: Api, upload_id: str, size: int) -> str:
    deadline = time.monotonic() + BASE_WAIT_SECONDS + size / WAIT_BYTES_PER_SECOND
    polls = int((deadline - time.monotonic()) / POLL_SECONDS) + 1
    for _ in range(polls):
        status = api.get(f"/uploads/{upload_id}")["status"]
        if status != "pending":
            return status
        time.sleep(POLL_SECONDS)
    pytest.fail(f"upload {upload_id} still pending after {polls} polls")


def _job_reason(db: psycopg.Connection, upload_id: str) -> str | None:
    row = db.execute(
        "SELECT reason FROM ingest.jobs WHERE upload_id = %s AND stage <> 'received' "
        "ORDER BY updated_at DESC LIMIT 1",
        (upload_id,),
    ).fetchone()
    return row[0] if row else None


def _item(db: psycopg.Connection, upload_id: str) -> dict[str, Any] | None:
    row = db.execute(
        """
        SELECT kind, media_type, sha256, size_bytes, extraction, crs, properties,
               ST_XMin(footprint), ST_YMin(footprint), ST_XMax(footprint), ST_YMax(footprint)
        FROM catalog.items WHERE id = %s
        """,
        (upload_id,),
    ).fetchone()
    if row is None:
        return None
    kind, media, sha, size, extraction, crs, properties, *box = row
    bbox = None if box[0] is None else tuple(box)
    return {
        "kind": kind,
        "media_type": media,
        "sha256": sha,
        "size": size,
        "extraction": extraction,
        "crs": crs,
        "properties": properties,
        "bbox": bbox,
    }


def _check_item(label: str, item: dict[str, Any], sample: samples.Sample, e: samples.Expect):
    check("ING-5", item["sha256"] == sample.sha256, f"{label}: checksum {item['sha256']}")
    for field in ("kind", "media_type", "extraction", "crs"):
        wanted = getattr(e, field)
        if wanted is not None:
            assert item[field] == wanted, f"{label}: {field} {item[field]!r}, wanted {wanted!r}"
    if e.bbox is not None:
        assert item["bbox"] is not None, f"{label}: no footprint, wanted {e.bbox}"
        off = max(abs(a - b) for a, b in zip(item["bbox"], e.bbox, strict=True))
        assert off <= e.bbox_tolerance, f"{label}: footprint {item['bbox']}, wanted {e.bbox}"
    for key, wanted in e.properties.items():
        assert item["properties"].get(key) == wanted, (
            f"{label}: {key} = {item['properties'].get(key)!r}, wanted {wanted!r}"
        )


@pytest.mark.invariant("ING-2", "ING-5", "ING-6")
@pytest.mark.parametrize("sample", CORPUS, ids=[s.name for s in CORPUS])
def test_each_sample_reaches_its_outcome(sample: samples.Sample, http, collection, stack_db):
    db = stack_db
    if not sample.path.exists():
        pytest.fail(f"{sample.path} is missing: run `make samples`")
    api = Api(http, sign_in("carol"))
    for index, upload in enumerate(sample.uploads):  # in order: a duplicate follows its original
        label = f"{sample.name}[{index}]"
        body: dict[str, Any] = {
            "collection_id": collection(upload.collection),
            "filename": upload.filename or sample.name,
            "size_bytes": sample.size,
        }
        if declared := _declared(sample, upload):
            body["sha256"] = declared
        session = api.post("/uploads", body)
        upload_id = session["upload"]["id"]
        with httpx.Client(timeout=TIMEOUT) as storage:
            upload_blocks(storage, session["upload_url"], sample.path)

        status = _await_outcome(api, upload_id, sample.size)
        expect = upload.expect
        check("ING-2", status == expect.outcome, f"{label}: {status}, wanted {expect.outcome}")
        if expect.reason is not None:
            reason = _job_reason(db, upload_id)
            assert reason == expect.reason, f"{label}: reason {reason!r}, wanted {expect.reason!r}"
        item = _item(db, upload_id)
        check("ING-2", (item is not None) == (status == "cataloged"), f"{label}: item {item}")
        if item is not None:
            _check_item(label, item, sample, expect)
