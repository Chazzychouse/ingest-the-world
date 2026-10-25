"""The pipeline, one blob version at a time, against Postgres and both blob stores.

Each test lands bytes the way a browser would, hands the worker the event Event Grid would
send, and checks the job, the upload, the item and every container afterwards.
"""

import contextlib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from ingest_api.adapters.azure_blob import AzureBlobStore
from ingest_api.adapters.eicar_scanner import EICAR, EicarScanner
from ingest_api.events import parse_blob_created
from ingest_api.ports import BlobProps, BlobStore, Container, TransientStorageError
from ingest_api.uploads.models import Outcome
from ingest_api.worker.classify import Media
from ingest_api.worker.extract import MAX_EXTRACT_BYTES
from ingest_api.worker.formats import READERS
from ingest_api.worker.jobs import Reason
from ingest_api.worker.pipeline import SOURCE_ETAG, Pipeline, process
from tests.conftest import Landed
from tests.contracts.azurite import azurite
from tests.fakes import CrashAfter, MemoryBlobStore, SimClock, event_for
from tests.invariants import check
from tests.test_extract import DENVER, UTM_13N, geotiff
from tests.test_sandbox import segfaults

LANDING, RAW, QUARANTINE = Container.LANDING, Container.RAW, Container.QUARANTINE
TIFF = b"II*\x00" + bytes(range(256)) * 4


@pytest.fixture(params=["memory", "azurite"])
def store(request: pytest.FixtureRequest) -> BlobStore:
    if request.param == "memory":
        return MemoryBlobStore()
    return AzureBlobStore(azurite(*Container))


@pytest.fixture
def work(database_url, sandbox) -> Callable[..., Outcome]:
    """Runs the pipeline on the event announcing `props`, in the wire format."""

    def _work(store: BlobStore, props: BlobProps, *, readers=READERS) -> Outcome:
        event = parse_blob_created(event_for(props))
        pipeline = Pipeline(store, EicarScanner(store), SimClock(), sandbox, readers)
        with psycopg.connect(database_url) as conn:
            return process(conn, pipeline, event)

    return _work


def _job(db, props: BlobProps) -> tuple[str, str | None]:
    row = db.execute(
        "SELECT stage, reason FROM ingest.jobs WHERE blob_name = %s AND etag = %s",
        (props.name, props.etag),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _status(db, upload_id: UUID) -> str:
    return db.execute("SELECT status FROM ingest.uploads WHERE id = %s", (upload_id,)).fetchone()[0]


def _item(db, upload_id: UUID) -> tuple[str, str, int, str] | None:
    return db.execute(
        "SELECT kind, sha256, size_bytes, raw_blob FROM catalog.items WHERE id = %s", (upload_id,)
    ).fetchone()


def _audits(db, props: BlobProps) -> list[tuple[str, str, str, str | None, str | None]]:
    """The pipeline's audit events for one version: actor, action, outcome, marking, reason."""
    return db.execute(
        """
        SELECT actor_sub, action, outcome, marking, detail ->> 'reason' FROM audit.events
        WHERE request_id = %s ORDER BY id
        """,
        (f"{props.name}@{props.etag}",),
    ).fetchall()


@dataclass(frozen=True)
class Served:
    """What the API makes of an upload once the pipeline has decided it."""

    client: TestClient
    auth: Callable[[str], dict[str, str]]

    def item(self, upload_id: UUID) -> dict[str, Any]:
        response = self.client.get(f"/items/{upload_id}", headers=self.auth("carol"))
        assert response.status_code == 200, response.text
        return response.json()

    def anywhere(self, upload_id: UUID) -> bool:
        """Whether any item route serves it, even to an Admin cleared for CUI."""
        admin = self.auth("alice")
        read = self.client.get(f"/items/{upload_id}", headers=admin)
        search = self.client.post("/search", json={"ids": [str(upload_id)]}, headers=admin)
        assert search.status_code == 200, search.text
        return read.status_code != 404 or bool(search.json()["features"])

    def reason(self, upload_id: UUID) -> str | None:
        """Why the pipeline decided as it did, as the uploader sees it."""
        return self.client.get(f"/uploads/{upload_id}", headers=self.auth("carol")).json()["reason"]


@pytest.fixture
def served(client, auth) -> Served:
    return Served(client, auth)


def _raw(store: BlobStore, name: str) -> bytes | None:
    props = store.head(RAW, name)
    return None if props is None else store.read_range(RAW, name, 0, props.size)


@pytest.mark.invariant("ING-2", "ING-5")
def test_a_clean_upload_is_cataloged_from_its_raw_copy(store, start, database_url, db, work):
    landed = start(len(TIFF))
    props = store.put(LANDING, landed.name, TIFF)

    assert work(store, props) == "cataloged"
    assert _job(db, props) == ("cataloged", None)
    assert _status(db, landed.upload_id) == "cataloged"
    sha = hashlib.sha256(TIFF).hexdigest()
    assert _item(db, landed.upload_id) == ("raster", sha, len(TIFF), landed.name)
    assert _raw(store, landed.name) == TIFF
    assert store.head(LANDING, landed.name) is None


@pytest.mark.invariant("ING-6")
def test_a_geotiff_is_served_with_its_footprint(store, start, db, tmp_path, work, served):
    data = geotiff(tmp_path)
    landed = start(len(data))
    props = store.put(LANDING, landed.name, data)

    assert work(store, props) == "cataloged"
    item = served.item(landed.upload_id)
    properties = item["properties"]
    assert (properties["ingest:extraction"], properties["proj:code"]) == ("extracted", UTM_13N)
    assert (properties["ingest:kind"], properties["raster:band_count"]) == ("raster", 1)
    west, _, _, north = item["bbox"]
    assert (round(west, 3), round(north, 3)) == (DENVER[0], round(DENVER[3], 3))
    assert item["geometry"]["type"] == "Polygon"
    extracted = db.execute(
        "SELECT extracted_bytes FROM catalog.items WHERE id = %s", (landed.upload_id,)
    ).fetchone()[0]
    assert extracted <= MAX_EXTRACT_BYTES < len(data)


def test_bytes_that_dont_parse_are_cataloged_without_metadata(store, start, database_url, db, work):
    landed = start(len(TIFF))  # a TIFF signature over noise
    props = store.put(LANDING, landed.name, TIFF)

    assert work(store, props) == "cataloged"
    row = db.execute(
        "SELECT extraction, footprint, properties FROM catalog.items WHERE id = %s",
        (landed.upload_id,),
    ).fetchone()
    assert row == ("unreadable", None, {})


@pytest.mark.invariant("ING-2", "ING-8")
def test_a_file_that_crashes_its_reader_is_cataloged_without_metadata(store, start, db, work):
    """A poison pill: before the sandbox, it killed the worker on every delivery, the message
    dead-lettered and the version never reached an outcome."""
    landed = start(len(TIFF))
    props = store.put(LANDING, landed.name, TIFF)

    assert work(store, props, readers={**READERS, Media.TIFF: segfaults}) == "cataloged"
    row = db.execute(
        "SELECT extraction, extracted_bytes > 0, properties FROM catalog.items WHERE id = %s",
        (landed.upload_id,),
    ).fetchone()
    assert row == ("failed", True, {})
    assert store.head(LANDING, landed.name) is None


@pytest.mark.invariant("ING-1")
def test_redelivering_an_event_changes_nothing(store, start, database_url, db, work):
    landed = start(len(TIFF))
    props = store.put(LANDING, landed.name, TIFF)
    work(store, props)
    before = (_item(db, landed.upload_id), store.head(RAW, landed.name))

    assert work(store, props) == "cataloged"
    assert (_item(db, landed.upload_id), store.head(RAW, landed.name)) == before


@pytest.mark.invariant("ING-4")
def test_an_infected_upload_is_quarantined_and_never_served(store, start, db, work, served):
    infected = EICAR + b"\n"
    landed = start(len(infected), filename="invoice.pdf")
    props = store.put(LANDING, landed.name, infected)

    assert work(store, props) == "quarantined"
    assert _job(db, props) == ("quarantined", "EICAR-Test-File")
    assert _status(db, landed.upload_id) == "quarantined"
    assert served.reason(landed.upload_id) == "EICAR-Test-File"
    assert _item(db, landed.upload_id) is None
    check("ING-4", not served.anywhere(landed.upload_id), "a quarantined file is served")
    assert store.head(RAW, landed.name) is None
    held = store.head(QUARANTINE, f"{props.etag}/{landed.name}")
    assert held is not None and held.metadata[SOURCE_ETAG] == props.etag
    assert store.head(LANDING, landed.name) is None


@pytest.mark.invariant("ING-2")
def test_bytes_of_the_wrong_size_reject_the_upload(store, start, database_url, db, work):
    landed = start(len(TIFF) + 1)
    props = store.put(LANDING, landed.name, TIFF)

    assert work(store, props) == "rejected"
    assert _job(db, props) == ("rejected", Reason.SIZE_MISMATCH)
    assert _status(db, landed.upload_id) == "rejected"
    assert store.head(LANDING, landed.name) is None


@pytest.mark.invariant("ING-2", "ING-5")
def test_a_wrong_checksum_rejects_and_leaves_no_raw_copy(store, start, database_url, db, work):
    landed = start(len(TIFF), sha256="0" * 64)
    props = store.put(LANDING, landed.name, TIFF)

    assert work(store, props) == "rejected"
    assert _job(db, props) == ("rejected", Reason.CHECKSUM_MISMATCH)
    assert (_item(db, landed.upload_id), store.head(RAW, landed.name)) == (None, None)


def test_a_blob_nobody_uploaded_is_rejected_and_cleared(store, database_url, db, work):
    props = store.put(LANDING, "site-survey-alpha/0000/stray.bin", b"x")

    assert work(store, props) == "rejected"
    assert _job(db, props) == ("rejected", Reason.NO_UPLOAD)
    assert store.head(LANDING, props.name) is None


@pytest.mark.invariant("ING-2")
def test_an_overwritten_version_loses_to_the_newer_one(store, start, database_url, db, work):
    landed = start(len(TIFF))
    older = store.put(LANDING, landed.name, b"II*\x00" + b"\x01" * (len(TIFF) - 4))
    newer = store.put(LANDING, landed.name, TIFF)

    assert work(store, older) == "rejected"
    assert _job(db, older) == ("rejected", Reason.SUPERSEDED)
    assert store.head(LANDING, landed.name) == newer  # left for its own event
    assert work(store, newer) == "cataloged"
    assert _raw(store, landed.name) == TIFF


@pytest.mark.invariant("ING-3", "ING-5")
def test_a_promoted_but_uncataloged_older_version_gives_way(store, start, database_url, db, work):
    landed = start(len(TIFF))
    older = store.put(LANDING, landed.name, b"II*\x00" + b"\x01" * (len(TIFF) - 4))
    # An earlier worker promoted the older version and died before cataloging it.
    store.copy(
        (LANDING, landed.name),
        (RAW, landed.name),
        if_match=older.etag,
        metadata={SOURCE_ETAG: older.etag},
    )
    db.execute(
        "INSERT INTO ingest.jobs (blob_name, etag, upload_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, now(), now())",
        (landed.name, older.etag, landed.upload_id),
    )
    newer = store.put(LANDING, landed.name, TIFF)

    assert work(store, newer) == "cataloged"
    assert _job(db, older) == ("rejected", Reason.SUPERSEDED)
    assert _raw(store, landed.name) == TIFF
    assert work(store, older) == "rejected"  # its late redelivery
    assert _raw(store, landed.name) == TIFF


@pytest.mark.invariant("ING-2", "ING-3")
def test_once_cataloged_an_upload_takes_no_new_version(store, start, database_url, db, work):
    landed = start(len(TIFF))
    first = store.put(LANDING, landed.name, TIFF)
    work(store, first)
    item = _item(db, landed.upload_id)
    again = store.put(LANDING, landed.name, b"II*\x00" + b"\x02" * (len(TIFF) - 4))

    assert work(store, again) == "rejected"
    assert _job(db, again) == ("rejected", Reason.UPLOAD_CLOSED)
    assert (_item(db, landed.upload_id), _raw(store, landed.name)) == (item, TIFF)
    assert store.head(LANDING, landed.name) is None


def _storage(store: MemoryBlobStore, landed: Landed, etag: str) -> dict[str, list[str]]:
    """Every container's blob names, with this upload's name and etag made generic."""
    return {
        c: [n.replace(landed.name, "NAME").replace(etag, "ETAG") for n in store.names(c)]
        for c in Container
    }


@pytest.mark.invariant("ING-1", "ING-5")
@pytest.mark.parametrize("payload", ["clean", "infected", "wrong-checksum"])
def test_a_job_interrupted_after_any_step_finishes_the_same(start, payload, work, new_collection):
    """Crashes the job right after each storage call in turn, then lets the retry run. Each run
    uploads into a collection of its own, so none is a duplicate of another (ING-9)."""
    data = EICAR if payload == "infected" else TIFF
    sha = "0" * 64 if payload == "wrong-checksum" else None
    reference = MemoryBlobStore()
    ref = start(len(data), sha256=sha, collection=new_collection())
    ref_props = reference.put(LANDING, ref.name, data)
    expected = (work(reference, ref_props), _storage(reference, ref, ref_props.etag))

    interrupted = 0
    for calls in range(1, 30):
        memory = MemoryBlobStore()
        landed = start(len(data), sha256=sha, collection=new_collection())
        props = memory.put(LANDING, landed.name, data)
        crashing = CrashAfter(memory, calls)
        with contextlib.suppress(TransientStorageError):  # the injected crash
            work(crashing, props)
        if not crashing.crashed:
            break  # the job needs fewer calls than this, so every one has been interrupted
        interrupted += 1
        outcome = work(memory, props)
        assert (outcome, _storage(memory, landed, props.etag)) == expected
    assert interrupted >= 4, "the job should take several storage calls"


# (payload, bytes declared beyond the real size, the audit event expected)
AUDITED = [
    ("clean", 0, ("item.catalog", "allowed", None)),
    ("infected", 0, ("upload.quarantine", "denied", "EICAR-Test-File")),
    ("clean", 1, ("upload.reject", "denied", Reason.SIZE_MISMATCH)),
]


@pytest.mark.invariant("ACC-5")
@pytest.mark.parametrize("case", AUDITED, ids=["cataloged", "quarantined", "rejected"])
def test_every_pipeline_outcome_is_audited_once(store, start, db, work, case):
    payload, declared, expected = case
    data = EICAR if payload == "infected" else TIFF
    landed = start(len(data) + declared)
    props = store.put(LANDING, landed.name, data)

    work(store, props)
    work(store, props)  # a redelivery decides nothing, so it records nothing
    action, outcome, reason = expected
    check(
        "ACC-5",
        _audits(db, props) == [("system:pipeline", action, outcome, "UNCLASSIFIED", reason)],
        f"audit events for {payload}: {_audits(db, props)}",
    )


@pytest.mark.invariant("ACC-5")
def test_a_stray_blob_is_audited_without_a_marking(store, db, work):
    props = store.put(LANDING, "site-survey-alpha/0000/audited.bin", b"x")

    work(store, props)
    assert _audits(db, props) == [
        ("system:pipeline", "upload.reject", "denied", None, Reason.NO_UPLOAD)
    ]


@pytest.mark.invariant("ING-9", "ING-5", "ING-4")
def test_the_same_bytes_twice_in_one_collection_are_a_duplicate(store, start, db, work, served):
    first, second = start(len(TIFF)), start(len(TIFF))
    work(store, store.put(LANDING, first.name, TIFF))
    item = _item(db, first.upload_id)
    props = store.put(LANDING, second.name, TIFF)

    assert work(store, props) == "rejected"
    check("ING-9", _job(db, props) == ("rejected", Reason.DUPLICATE), f"{_job(db, props)}")
    assert _status(db, second.upload_id) == "rejected"
    assert served.reason(second.upload_id) == Reason.DUPLICATE
    assert (_item(db, first.upload_id), _item(db, second.upload_id)) == (item, None)
    check("ING-4", not served.anywhere(second.upload_id), "a rejected file is served")
    assert served.anywhere(first.upload_id)
    check("ING-5", store.head(RAW, second.name) is None, "the duplicate's raw copy was kept")
    assert store.head(LANDING, second.name) is None


@pytest.mark.invariant("ING-9")
def test_the_same_bytes_in_another_collection_are_a_new_item(
    store, start, db, work, new_collection
):
    first, second = start(len(TIFF)), start(len(TIFF), collection=new_collection())
    work(store, store.put(LANDING, first.name, TIFF))

    assert work(store, store.put(LANDING, second.name, TIFF)) == "cataloged"
    assert _item(db, second.upload_id) is not None
