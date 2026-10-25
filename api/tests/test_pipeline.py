"""The pipeline, one blob version at a time, against Postgres and both blob stores.

Each test lands bytes the way a browser would, hands the worker the event Event Grid would
send, and checks the job, the upload, the item and every container afterwards.
"""

import contextlib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest

from ingest_api.adapters.azure_blob import AzureBlobStore
from ingest_api.adapters.eicar_scanner import EICAR, EicarScanner
from ingest_api.events import parse_blob_created
from ingest_api.ports import BlobProps, BlobStore, Container, TransientStorageError
from ingest_api.uploads.models import Outcome
from ingest_api.worker.extract import MAX_EXTRACT_BYTES
from ingest_api.worker.jobs import Reason
from ingest_api.worker.pipeline import SOURCE_ETAG, Pipeline, process
from tests.contracts.azurite import azurite
from tests.fakes import CrashAfter, MemoryBlobStore, SimClock, event_for
from tests.test_extract import DENVER, UTM_13N, geotiff

LANDING, RAW, QUARANTINE = Container.LANDING, Container.RAW, Container.QUARANTINE
TIFF = b"II*\x00" + bytes(range(256)) * 4


@dataclass(frozen=True, slots=True)
class Landed:
    upload_id: UUID
    name: str


@pytest.fixture(params=["memory", "azurite"])
def store(request: pytest.FixtureRequest) -> BlobStore:
    if request.param == "memory":
        return MemoryBlobStore()
    return AzureBlobStore(azurite(*Container))


@pytest.fixture
def start(client, auth, db) -> Callable[..., Landed]:
    """Starts an upload as carol and returns where its bytes go."""

    def _start(size: int, *, sha256: str | None = None, filename: str = "ortho.tif") -> Landed:
        body = {"collection_id": "site-survey-alpha", "filename": filename, "size_bytes": size}
        if sha256:
            body["sha256"] = sha256
        upload_id = client.post("/uploads", json=body, headers=auth("carol")).json()["upload"]["id"]
        row = db.execute("SELECT blob_name FROM ingest.uploads WHERE id = %s", (upload_id,))
        return Landed(UUID(upload_id), row.fetchone()[0])

    return _start


def _work(database_url: str, store: BlobStore, props: BlobProps) -> Outcome:
    """Runs the pipeline on the event announcing `props`, in the wire format."""
    event = parse_blob_created(event_for(props))
    pipeline = Pipeline(store=store, scanner=EicarScanner(store), clock=SimClock())
    with psycopg.connect(database_url) as conn:
        return process(conn, pipeline, event)


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


def _raw(store: BlobStore, name: str) -> bytes | None:
    props = store.head(RAW, name)
    return None if props is None else store.read_range(RAW, name, 0, props.size)


@pytest.mark.invariant("ING-2", "ING-5")
def test_a_clean_upload_is_cataloged_from_its_raw_copy(store, start, database_url, db):
    landed = start(len(TIFF))
    props = store.put(LANDING, landed.name, TIFF)

    assert _work(database_url, store, props) == "cataloged"
    assert _job(db, props) == ("cataloged", None)
    assert _status(db, landed.upload_id) == "cataloged"
    sha = hashlib.sha256(TIFF).hexdigest()
    assert _item(db, landed.upload_id) == ("raster", sha, len(TIFF), landed.name)
    assert _raw(store, landed.name) == TIFF
    assert store.head(LANDING, landed.name) is None


@pytest.mark.invariant("ING-6")
def test_a_geotiff_is_cataloged_with_its_footprint(store, start, database_url, db, tmp_path):
    data = geotiff(tmp_path)
    landed = start(len(data))
    props = store.put(LANDING, landed.name, data)

    assert _work(database_url, store, props) == "cataloged"
    row = db.execute(
        """
        SELECT extraction, crs, ST_XMin(footprint), ST_YMax(footprint), extracted_bytes,
               properties ->> 'raster:band_count'
        FROM catalog.items WHERE id = %s
        """,
        (landed.upload_id,),
    ).fetchone()
    assert row is not None
    extraction, crs, west, north, extracted, bands = row
    assert (extraction, crs, bands) == ("extracted", UTM_13N, "1")
    assert (round(west, 3), round(north, 3)) == (DENVER[0], round(DENVER[3], 3))
    assert extracted <= MAX_EXTRACT_BYTES < len(data)


def test_bytes_that_dont_parse_are_cataloged_without_metadata(store, start, database_url, db):
    landed = start(len(TIFF))  # a TIFF signature over noise
    props = store.put(LANDING, landed.name, TIFF)

    assert _work(database_url, store, props) == "cataloged"
    row = db.execute(
        "SELECT extraction, footprint, properties FROM catalog.items WHERE id = %s",
        (landed.upload_id,),
    ).fetchone()
    assert row == ("unreadable", None, {})


@pytest.mark.invariant("ING-1")
def test_redelivering_an_event_changes_nothing(store, start, database_url, db):
    landed = start(len(TIFF))
    props = store.put(LANDING, landed.name, TIFF)
    _work(database_url, store, props)
    before = (_item(db, landed.upload_id), store.head(RAW, landed.name))

    assert _work(database_url, store, props) == "cataloged"
    assert (_item(db, landed.upload_id), store.head(RAW, landed.name)) == before


@pytest.mark.invariant("ING-4")
def test_an_infected_upload_is_quarantined_and_never_promoted(store, start, database_url, db):
    infected = EICAR + b"\n"
    landed = start(len(infected), filename="invoice.pdf")
    props = store.put(LANDING, landed.name, infected)

    assert _work(database_url, store, props) == "quarantined"
    assert _job(db, props) == ("quarantined", "EICAR-Test-File")
    assert _status(db, landed.upload_id) == "quarantined"
    assert _item(db, landed.upload_id) is None
    assert store.head(RAW, landed.name) is None
    held = store.head(QUARANTINE, f"{props.etag}/{landed.name}")
    assert held is not None and held.metadata[SOURCE_ETAG] == props.etag
    assert store.head(LANDING, landed.name) is None


@pytest.mark.invariant("ING-2")
def test_bytes_of_the_wrong_size_reject_the_upload(store, start, database_url, db):
    landed = start(len(TIFF) + 1)
    props = store.put(LANDING, landed.name, TIFF)

    assert _work(database_url, store, props) == "rejected"
    assert _job(db, props) == ("rejected", Reason.SIZE_MISMATCH)
    assert _status(db, landed.upload_id) == "rejected"
    assert store.head(LANDING, landed.name) is None


@pytest.mark.invariant("ING-2", "ING-5")
def test_a_wrong_checksum_rejects_and_leaves_no_raw_copy(store, start, database_url, db):
    landed = start(len(TIFF), sha256="0" * 64)
    props = store.put(LANDING, landed.name, TIFF)

    assert _work(database_url, store, props) == "rejected"
    assert _job(db, props) == ("rejected", Reason.CHECKSUM_MISMATCH)
    assert (_item(db, landed.upload_id), store.head(RAW, landed.name)) == (None, None)


def test_a_blob_nobody_uploaded_is_rejected_and_cleared(store, database_url, db):
    props = store.put(LANDING, "site-survey-alpha/0000/stray.bin", b"x")

    assert _work(database_url, store, props) == "rejected"
    assert _job(db, props) == ("rejected", Reason.NO_UPLOAD)
    assert store.head(LANDING, props.name) is None


@pytest.mark.invariant("ING-2")
def test_an_overwritten_version_loses_to_the_newer_one(store, start, database_url, db):
    landed = start(len(TIFF))
    older = store.put(LANDING, landed.name, b"II*\x00" + b"\x01" * (len(TIFF) - 4))
    newer = store.put(LANDING, landed.name, TIFF)

    assert _work(database_url, store, older) == "rejected"
    assert _job(db, older) == ("rejected", Reason.SUPERSEDED)
    assert store.head(LANDING, landed.name) == newer  # left for its own event
    assert _work(database_url, store, newer) == "cataloged"
    assert _raw(store, landed.name) == TIFF


@pytest.mark.invariant("ING-3", "ING-5")
def test_a_promoted_but_uncataloged_older_version_gives_way(store, start, database_url, db):
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

    assert _work(database_url, store, newer) == "cataloged"
    assert _job(db, older) == ("rejected", Reason.SUPERSEDED)
    assert _raw(store, landed.name) == TIFF
    assert _work(database_url, store, older) == "rejected"  # its late redelivery
    assert _raw(store, landed.name) == TIFF


@pytest.mark.invariant("ING-2", "ING-3")
def test_once_cataloged_an_upload_takes_no_new_version(store, start, database_url, db):
    landed = start(len(TIFF))
    first = store.put(LANDING, landed.name, TIFF)
    _work(database_url, store, first)
    item = _item(db, landed.upload_id)
    again = store.put(LANDING, landed.name, b"II*\x00" + b"\x02" * (len(TIFF) - 4))

    assert _work(database_url, store, again) == "rejected"
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
def test_a_job_interrupted_after_any_step_finishes_the_same(start, database_url, payload):
    """Crashes the job right after each storage call in turn, then lets the retry run."""
    data = EICAR if payload == "infected" else TIFF
    sha = "0" * 64 if payload == "wrong-checksum" else None
    reference = MemoryBlobStore()
    ref = start(len(data), sha256=sha)
    ref_props = reference.put(LANDING, ref.name, data)
    expected = (_work(database_url, reference, ref_props), _storage(reference, ref, ref_props.etag))

    interrupted = 0
    for calls in range(1, 30):
        memory = MemoryBlobStore()
        landed = start(len(data), sha256=sha)
        props = memory.put(LANDING, landed.name, data)
        crashing = CrashAfter(memory, calls)
        with contextlib.suppress(TransientStorageError):  # the injected crash
            _work(database_url, crashing, props)
        if not crashing.crashed:
            break  # the job needs fewer calls than this, so every one has been interrupted
        interrupted += 1
        outcome = _work(database_url, memory, props)
        assert (outcome, _storage(memory, landed, props.etag)) == expected
    assert interrupted >= 4, "the job should take several storage calls"
