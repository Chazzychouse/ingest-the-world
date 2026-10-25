"""Stateful simulation of the ingest pipeline under storage and queue faults.

Hypothesis starts uploads through the real API, lands bytes for them (sometimes the wrong
size, a wrong checksum, the EICAR test file, or a second version over the first), announces
them on a faulty queue (duplicates, reordering, lost completions), and runs the worker against
a faulty blob store (transient errors, writes whose acknowledgement is lost) and Postgres.

After every step it checks that catalog and storage agree (ING-5), that nothing flagged left
quarantine (ING-4), that cataloged originals never change (ING-3), that no outcome ever
changes or contradicts its bytes (ING-2), and that extraction kept to its budget (ING-6). At
the end it turns faults off, drains the queue, sweeps the dead letters as the worker does (giving
up their versions), and checks that every version reached exactly one outcome (ING-2); then it
delivers every event again and checks nothing moved (ING-1).
"""

import hashlib
import random
from dataclasses import dataclass

import psycopg
import pytest
from fastapi.testclient import TestClient
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from ingest_api.adapters.eicar_scanner import EICAR, EicarScanner
from ingest_api.ports import Container
from ingest_api.worker.extract import MAX_EXTRACT_BYTES, Sandbox
from ingest_api.worker.pipeline import Pipeline
from ingest_api.worker.runner import Retry, run_once, sweep_once
from tests.conftest import make_app, reset_catalog
from tests.fakes import Faults, MemoryBlobStore, SimClock, SimQueue, event_for
from tests.invariants import check
from tests.tokens import TestIdp

LANDING, RAW = Container.LANDING, Container.RAW
COLLECTIONS = ["site-survey-alpha", "bio-lab-readings", "range-ops-cui"]
ADMIN = {"roles": ["Admin", "Clearance.CUI"], "groups": []}
# Deliveries before the fault-free drain gives up; far more than any run needs.
MAX_DRAIN_ROUNDS = 200

payloads = st.one_of(
    st.binary(min_size=1, max_size=64).map(lambda b: b"II*\x00" + b),  # a "TIFF"
    st.binary(min_size=1, max_size=64),
    st.just(EICAR),
    st.sampled_from([b"II*\x00same", b"same again"]),  # often uploaded twice: duplicates
)
faults = st.builds(
    Faults,
    transient=st.sampled_from([0.0, 0.1, 0.3]),
    lost_ack=st.sampled_from([0.0, 0.2]),
    duplicate=st.sampled_from([0.0, 0.3]),
    reorder=st.sampled_from([0.0, 0.5]),
    lost_complete=st.sampled_from([0.0, 0.2]),
)


@dataclass(frozen=True, slots=True)
class Upload:
    id: str
    name: str
    declared_size: int
    declared_sha: str | None


@dataclass(frozen=True, slots=True)
class Version:
    upload: Upload
    data: bytes
    event: dict[str, object]

    @property
    def infected(self) -> bool:
        return self.data.startswith(EICAR)

    def may_catalog(self) -> bool:
        sha = hashlib.sha256(self.data).hexdigest()
        return (
            not self.infected
            and len(self.data) == self.upload.declared_size
            and self.upload.declared_sha in (None, sha)
        )


class PipelineMachine(RuleBasedStateMachine):
    uploads = Bundle("uploads")

    def __init__(
        self,
        client: TestClient,
        db: psycopg.Connection,
        url: str,
        idp: TestIdp,
        sandbox: Sandbox,
    ):
        super().__init__()
        self.client, self.db, self.url, self.idp = client, db, url, idp
        self.sandbox = sandbox
        reset_catalog(db)
        self.versions: dict[tuple[str, str], Version] = {}  # (name, etag), in commit order
        self.outcomes: dict[tuple[str, str], str] = {}  # first outcome seen per version
        self.item_shas: dict[str, str] = {}  # first checksum seen per item

    @initialize(target=uploads, seed=st.integers(0, 2**32 - 1), faults=faults, data=payloads)
    def start(self, seed: int, faults: Faults, data: bytes) -> Upload:
        """Builds the faulty world, and one upload so every run has something to process."""
        rng = random.Random(seed)
        self.clock = SimClock()
        self.faults = faults
        self.store = MemoryBlobStore(rng, faults)
        # Few deliveries, so faults alone exhaust some versions and the give-up paths run.
        self.queue = SimQueue(
            clock=self.clock, rng=rng, faults=faults, lock_seconds=60, max_delivery_count=3
        )
        self.pipeline = Pipeline(self.store, EicarScanner(self.store), self.clock, self.sandbox)
        return self._start_upload(COLLECTIONS[0], data, 0, "right")

    # -- browser and Event Grid ----------------------------------------------------------

    def _start_upload(self, collection, data, size_off_by, sha) -> Upload:
        declared_sha = {
            "none": None,
            "right": hashlib.sha256(data).hexdigest(),
            "wrong": "0" * 64,
        }[sha]
        body = {"collection_id": collection, "filename": "f.tif", "size_bytes": len(data)}
        body["size_bytes"] += size_off_by
        if declared_sha:
            body["sha256"] = declared_sha
        response = self.client.post(
            "/uploads", json=body, headers=self.idp.bearer("s-admin", **ADMIN)
        )
        assert response.status_code == 201, response.text
        upload_id = response.json()["upload"]["id"]
        [(name,)] = self.db.execute(
            "SELECT blob_name FROM ingest.uploads WHERE id = %s", (upload_id,)
        ).fetchall()
        upload = Upload(upload_id, name, body["size_bytes"], declared_sha)
        self._commit(upload, data)
        return upload

    def _commit(self, upload: Upload, data: bytes) -> None:
        """The browser commits bytes (a new version if some were committed before), and Event
        Grid announces them."""
        faults, self.store.faults = self.store.faults, Faults.none()  # the browser's write
        props = self.store.put(LANDING, upload.name, data)
        self.store.faults = faults
        event = event_for(props)
        self.versions[(props.name, props.etag)] = Version(upload, data, event)
        self.queue.send(event)

    @rule(
        target=uploads,
        collection=st.sampled_from(COLLECTIONS),
        data=payloads,
        size_off_by=st.sampled_from([0, 0, 0, 1]),
        sha=st.sampled_from(["none", "none", "right", "wrong"]),
    )
    def upload(self, collection, data, size_off_by, sha):
        """Someone uploads a file: starts the session and commits its bytes."""
        return self._start_upload(collection, data, size_off_by, sha)

    @rule(upload=uploads, data=payloads, fit=st.booleans())
    def commit_again(self, upload, data, fit):
        """The same session's URL is used again, committing a second version over the first.
        With `fit`, the bytes are cut or padded to the declared size, so the newer version gets
        past the size check and its checksum and scan decide it."""
        if fit:
            data = data[: upload.declared_size].ljust(upload.declared_size, b"\x00")
        self._commit(upload, data)

    @rule(data=st.data())
    def redeliver(self, data):
        if self.versions:
            version = data.draw(st.sampled_from(list(self.versions.values())))
            self.queue.send(version.event)

    # -- the worker and time ---------------------------------------------------------------

    @rule(batch=st.integers(1, 3))
    def work(self, batch):
        run_once(
            self.queue,
            lambda: psycopg.connect(self.url),
            self.pipeline,
            retry=Retry(self.queue.max_delivery_count),
            batch=batch,
        )

    @rule()
    def locks_expire(self):
        self.clock.advance(61)

    # -- checks ----------------------------------------------------------------------------

    def _items(self) -> dict[str, tuple[str, str]]:
        rows = self.db.execute("SELECT id, raw_blob, sha256 FROM catalog.items").fetchall()
        return {str(i): (raw, sha) for i, raw, sha in rows}

    @invariant()
    def extraction_stays_within_its_budget(self):
        rows = self.db.execute("SELECT id, extracted_bytes FROM catalog.items").fetchall()
        for item_id, extracted in rows:
            check("ING-6", extracted <= MAX_EXTRACT_BYTES, f"{item_id} read {extracted} bytes")

    def _jobs(self) -> dict[tuple[str, str], tuple[str, str | None]]:
        rows = self.db.execute("SELECT blob_name, etag, stage, reason FROM ingest.jobs")
        return {(n, e): (stage, reason) for n, e, stage, reason in rows}

    @invariant()
    def catalog_and_storage_agree(self):
        items = self._items()
        jobs = self._jobs()
        for item_id, (raw, sha) in items.items():
            content = self.store.content(RAW, raw) if raw in self.store.names(RAW) else None
            check("ING-5", content is not None, f"item {item_id} has no raw blob {raw}")
            check("ING-5", hashlib.sha256(content or b"").hexdigest() == sha, f"{raw} hash")
            check("ING-3", self.item_shas.setdefault(item_id, sha) == sha, f"{item_id} changed")
            check("ING-4", not (content or b"").startswith(EICAR), f"item {item_id} infected")
        in_flight = {name for (name, _), (stage, _) in jobs.items() if stage == "received"}
        cataloged = {raw for raw, _ in items.values()}
        for name in self.store.names(RAW):
            check("ING-5", name in cataloged | in_flight, f"orphan raw blob {name}")
            check("ING-4", not self.store.content(RAW, name).startswith(EICAR), f"{name} raw")

    @invariant()
    def every_outcome_is_audited_once(self):
        rows = self.db.execute(
            """
            SELECT j.blob_name, j.etag, j.stage, count(e.id), min(e.action), min(e.actor_sub)
            FROM ingest.jobs j
            LEFT JOIN audit.events e ON e.request_id = j.blob_name || '@' || j.etag
            WHERE j.stage <> 'received'
            GROUP BY j.blob_name, j.etag, j.stage
            """
        ).fetchall()
        for name, etag, stage, events, action, actor in rows:
            key = f"{name}@{etag}"
            check("ACC-5", events == 1, f"{key} is {stage} with {events} audit events")
            check("ACC-5", actor == "system:pipeline", f"{key} audited as {actor}")
            check("ACC-5", (action == "item.catalog") == (stage == "cataloged"), f"{key} {action}")

    @invariant()
    def one_item_per_content_per_collection(self):
        rows = self.db.execute(
            "SELECT collection_id, sha256, count(*) FROM catalog.items "
            "GROUP BY collection_id, sha256 HAVING count(*) > 1"
        ).fetchall()
        check("ING-9", rows == [], f"items sharing a checksum in a collection: {rows}")

    def _has_item_like(self, version: Version) -> bool:
        """Whether another upload's item in this version's collection has its bytes."""
        sha = hashlib.sha256(version.data).hexdigest()
        row = self.db.execute(
            "SELECT 1 FROM catalog.items i JOIN ingest.uploads u ON u.id = %s "
            "WHERE i.collection_id = u.collection_id AND i.sha256 = %s AND i.id <> u.id",
            (version.upload.id, sha),
        ).fetchone()
        return row is not None

    @invariant()
    def outcomes_are_final_and_honest(self):
        for key, (stage, reason) in self._jobs().items():
            if stage == "received":
                continue
            first = self.outcomes.setdefault(key, stage)
            check("ING-2", first == stage, f"{key} went from {first} to {stage}")
            version = self.versions.get(key)
            if version is None:
                continue
            if stage == "cataloged":
                check("ING-2", version.may_catalog(), f"{key} cataloged but invalid")
            if stage == "quarantined":
                check("ING-4", version.infected, f"{key} quarantined but clean")
            if version.infected:
                check("ING-4", stage != "cataloged", f"{key} infected yet cataloged")
            if reason == "duplicate":
                check("ING-9", self._has_item_like(version), f"{key} a duplicate of nothing")

    def teardown(self):
        if not hasattr(self, "store"):  # the run failed before `start`
            return
        self._drain()
        self._check_every_version_settled()
        before = self._snapshot()
        for version in self.versions.values():
            self.queue.send(version.event)
        self._drain()
        check("ING-1", self._snapshot() == before, "redelivering every event changed state")

    def _drain(self) -> None:
        """Faults off; run the worker until the queue is empty, then sweep the dead letters, as
        the worker does on a timer."""
        self.store.faults = self.queue.faults = Faults.none()
        for _ in range(MAX_DRAIN_ROUNDS):
            self.clock.advance(61)
            if len(self.queue) == 0:
                break
            self.work(batch=3)
        check("ING-2", len(self.queue) == 0, "the queue did not drain with faults off")
        for _ in range(MAX_DRAIN_ROUNDS):
            if (
                sweep_once(self.queue, lambda: psycopg.connect(self.url), self.pipeline, batch=3)
                == 0
            ):
                break
        check("ING-2", self.queue.dead_letters == [], "dead letters left after the sweep")

    def _check_every_version_settled(self) -> None:
        jobs = self._jobs()
        for key in self.versions:
            stage = jobs.get(key, ("missing", None))[0]
            check("ING-2", stage in ("cataloged", "quarantined", "rejected"), f"{key}: {stage}")
        pending = self.db.execute(
            "SELECT id FROM ingest.uploads u WHERE status = 'pending' "
            "AND EXISTS (SELECT 1 FROM ingest.jobs j WHERE j.upload_id = u.id)"
        ).fetchall()
        check("ING-2", pending == [], f"uploads with bytes but no outcome: {pending}")
        check("ING-2", self.store.names(LANDING) == [], "versions left in landing")
        cataloged = {raw for raw, _ in self._items().values()}
        check("ING-5", set(self.store.names(RAW)) == cataloged, "raw holds uncataloged blobs")

    def _snapshot(self):
        uploads = self.db.execute("SELECT id, status FROM ingest.uploads ORDER BY id").fetchall()
        storage = {c: {n: self.store.content(c, n) for n in self.store.names(c)} for c in Container}
        return self._jobs(), self._items(), uploads, storage


@pytest.mark.invariant("ACC-5", "ING-1", "ING-2", "ING-3", "ING-4", "ING-5", "ING-6", "ING-9")
def test_pipeline_simulation(fresh_database, idp, sandbox):
    url = fresh_database()
    with TestClient(make_app(url, idp)) as client, psycopg.connect(url, autocommit=True) as db:
        # Hypothesis accepts any factory here; its annotation only says `type`.
        run_state_machine_as_test(lambda: PipelineMachine(client, db, url, idp, sandbox))  # pyright: ignore[reportArgumentType]
