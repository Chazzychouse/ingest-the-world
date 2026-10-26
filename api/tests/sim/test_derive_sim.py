"""Stateful simulation of the derive stage under storage and queue faults.

Hypothesis catalogs items (raster or not, some whose originals a recipe refuses) and asks for
their derivatives in the same transaction, as the pipeline does; asks again for some; sends
requests for things that aren't items, and messages that aren't requests at all. It relays the
outbox to a faulty queue (duplicates, reordering, lost completions) and runs the derive stage
against a faulty blob store (transient errors, writes whose acknowledgement is lost), with
recipes that are deterministic, random, or refuse some originals.

After every step it checks that each asset row and its blob in `derived/` agree, that no blob
there lacks an asset or an unfinished job, and that no derive job's outcome changes (ING-12);
and that items and their originals never change and only items are derived (ING-11). At the end
it turns faults off, drains and sweeps the queue, checks every derivative reached an outcome,
delivers every request again and checks nothing moved (ING-12); then deletes every derivative,
asks again for all of them, and checks the rebuild matches (ING-7).
"""

import hashlib
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from ingest_api.items.kinds import Kind
from ingest_api.ports import Container
from ingest_api.uploads import store as uploads
from ingest_api.uploads.models import UploadIn
from ingest_api.worker import outbox
from ingest_api.worker.derive import Deriver, Reason, blob_name
from ingest_api.worker.recipe import Recipe
from ingest_api.worker.runner import Retry, derive_once, sweep_derive_once
from tests.conftest import reset_catalog
from tests.fakes import INLINE_RENDERER, Faults, MemoryBlobStore, SimClock, SimQueue
from tests.invariants import check
from tests.items import NewItem, catalog
from tests.recipes import Digest, DigestV2, Picky, Stamp

DERIVED, RAW = Container.DERIVED, Container.RAW
COLLECTION = "site-survey-alpha"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
MAX_DRAIN_ROUNDS = 200
RELAY_LIMIT = 3
# Outcomes that say the attempts ran out, not what the original makes: a rebuild may differ.
TRANSIENT = {Reason.RETRIES_EXHAUSTED, Reason.DEAD_LETTERED}

originals = st.one_of(
    st.binary(min_size=1, max_size=48),
    st.binary(max_size=16).map(lambda b: b"bad" + b),  # Picky refuses these
)
faults = st.builds(
    Faults,
    transient=st.sampled_from([0.0, 0.1, 0.3]),
    lost_ack=st.sampled_from([0.0, 0.2]),
    duplicate=st.sampled_from([0.0, 0.3]),
    reorder=st.sampled_from([0.0, 0.5]),
    lost_complete=st.sampled_from([0.0, 0.2]),
)

type Key = tuple[str, str, int]  # item, role, recipe version


@dataclass(frozen=True, slots=True)
class Item:
    id: uuid.UUID
    kind: Kind
    raw_blob: str
    content: bytes


class DeriveMachine(RuleBasedStateMachine):
    items = Bundle("items")

    def __init__(self, db: psycopg.Connection, url: str, scratch):
        super().__init__()
        self.db, self.url, self.scratch = db, url, scratch
        reset_catalog(db)
        self.cataloged: dict[uuid.UUID, Item] = {}
        self.item_rows: dict[str, tuple[Any, ...]] = {}  # each item's row as cataloged
        self.strays: list[dict[str, Any]] = []  # sent straight to the queue
        self.outcomes: dict[Key, str] = {}  # first outcome seen per derive job
        self.made = 0
        # Set while the relay or the stage runs: if one raises, its error is the finding, and
        # teardown's checks would only bury it.
        self.interrupted = False

    @initialize(seed=st.integers(0, 2**32 - 1), faults=faults)
    def start(self, seed: int, faults: Faults) -> None:
        rng = random.Random(seed)
        self.clock = SimClock(NOW)
        self.faults = faults
        self.store = MemoryBlobStore(rng, faults)
        # Few deliveries, so faults alone exhaust some requests and the give-up paths run.
        self.queue = SimQueue(
            clock=self.clock, rng=rng, faults=faults, lock_seconds=60, max_delivery_count=3
        )
        self.recipes: list[Recipe] = [Digest(), DigestV2(), Stamp(random.Random(seed)), Picky()]
        self.deriver = Deriver(self.store, self.clock, self.scratch, INLINE_RENDERER, self.recipes)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.url)

    # -- the pipeline and strangers -----------------------------------------------------------

    @rule(target=items, kind=st.sampled_from([Kind.RASTER, Kind.VECTOR]), content=originals)
    def catalog(self, kind: Kind, content: bytes) -> Item:
        """The pipeline catalogs an item and asks for its derivatives in one transaction. Each
        original is made unique, so none is a duplicate (ING-9)."""
        self.made += 1
        content = content + self.made.to_bytes(4, "big")
        new = NewItem(COLLECTION, f"f{self.made}", NOW, kind=kind, content=content)
        with self._connect() as conn:
            cataloged = catalog(conn, new)
            outbox.request_derive(conn, cataloged.id, now=self.clock.now())
        faults, self.store.faults = self.store.faults, Faults.none()  # the pipeline's copy
        self.store.put(RAW, cataloged.raw_blob, content)
        self.store.faults = faults
        item = Item(cataloged.id, kind, cataloged.raw_blob, content)
        self.cataloged[item.id] = item
        self.item_rows[str(item.id)] = self._item_rows()[str(item.id)]
        return item

    @rule(item=items)
    def ask_again(self, item: Item) -> None:
        """Another request for an item's derivatives, as a rebuild would send."""
        with self._connect() as conn:
            outbox.request_derive(conn, item.id, now=self.clock.now())

    @rule(target_kind=st.sampled_from(["upload", "nothing"]))
    def stray(self, target_kind: str) -> None:
        """A request for something that isn't an item: an upload with no item (as every
        quarantined or rejected one is), or an id nothing has."""
        upload_id = uuid.uuid4()
        if target_kind == "upload":
            upload = UploadIn(collection_id=COLLECTION, filename="x", size_bytes=1)
            expires = NOW + timedelta(hours=1)
            uploads.insert(
                self.db, upload_id, upload, created_by="s", created_at=NOW, url_expires_at=expires
            )
        body = {"item_id": str(upload_id)}
        self.strays.append(body)
        self.queue.send(body)

    @rule()
    def garbage(self) -> None:
        body = {"item_id": 5}
        self.strays.append(body)
        self.queue.send(body)

    # -- the relay, the worker and time ----------------------------------------------------

    @rule()
    def relay(self) -> None:
        self.interrupted = True
        with self._connect() as conn:
            outbox.relay(conn, self.queue, limit=RELAY_LIMIT, now=self.clock.now())
        self.interrupted = False

    @rule(batch=st.integers(1, 3))
    def work(self, batch: int) -> None:
        retry = Retry(self.queue.max_delivery_count)
        self.interrupted = True
        derive_once(self.queue, self._connect, self.deriver, retry=retry, batch=batch)
        self.interrupted = False

    @rule()
    def locks_expire(self) -> None:
        self.clock.advance(61)

    # -- checks ----------------------------------------------------------------------------

    def _item_rows(self) -> dict[str, tuple[Any, ...]]:
        rows = self.db.execute("SELECT i.id::text, i::text FROM catalog.items i").fetchall()
        return {item_id: (row,) for item_id, row in rows}

    def _assets(self) -> dict[Key, tuple[str, str, int, str, dict[str, Any]]]:
        rows = self.db.execute(
            "SELECT item_id::text, role, recipe_version, media_type, blob_name, size_bytes, "
            "sha256, lineage FROM catalog.assets"
        ).fetchall()
        return {(i, r, v): (m, n, size, sha, lineage) for i, r, v, m, n, size, sha, lineage in rows}

    def _jobs(self) -> dict[Key, tuple[str, str | None]]:
        rows = self.db.execute(
            "SELECT item_id::text, role, recipe_version, stage, reason FROM ingest.derive_jobs"
        ).fetchall()
        return {(i, r, v): (stage, reason) for i, r, v, stage, reason in rows}

    def _recipe(self, role: str, version: int) -> Recipe:
        return next(r for r in self.recipes if (r.role, r.version) == (role, version))

    @invariant()
    def assets_and_their_blobs_agree(self) -> None:
        if not hasattr(self, "store"):
            return
        jobs, assets = self._jobs(), self._assets()
        for key, (_, name, size, sha, _) in assets.items():
            present = name in self.store.names(DERIVED)
            check("ING-12", present, f"asset {key} has no blob {name}")
            content = self.store.content(DERIVED, name) if present else b""
            check(
                "ING-12",
                (len(content), hashlib.sha256(content).hexdigest()) == (size, sha),
                f"{key}",
            )
            check("ING-12", jobs.get(key, ("missing",))[0] == "derived", f"{key} job not derived")
        for key, (stage, _) in jobs.items():
            check("ING-12", (stage == "derived") == (key in assets), f"{key} {stage} vs its asset")
        unfinished = {
            blob_name(COLLECTION, uuid.UUID(item), self._recipe(role, version))
            for (item, role, version), (stage, _) in jobs.items()
            if stage == "received"
        }
        recorded = {name for _, name, *_ in assets.values()}
        for name in self.store.names(DERIVED):
            check("ING-12", name in recorded | unfinished, f"orphan derivative {name}")

    @invariant()
    def derive_outcomes_are_final(self) -> None:
        for key, (stage, _) in self._jobs().items():
            if stage != "received":
                first = self.outcomes.setdefault(key, stage)
                check("ING-12", first == stage, f"{key} went from {first} to {stage}")

    @invariant()
    def items_and_originals_never_change(self) -> None:
        if not hasattr(self, "store"):
            return
        rows = self._item_rows()
        check("ING-11", rows == self.item_rows, "deriving changed an item")
        for item in self.cataloged.values():
            content = self.store.content(RAW, item.raw_blob)
            check("ING-11", content == item.content, f"deriving changed {item.raw_blob}")

    @invariant()
    def only_items_are_derived_and_only_as_recipes_say(self) -> None:
        for item_id, role, _ in self._jobs():
            item = self.cataloged.get(uuid.UUID(item_id))
            check("ING-11", item is not None, f"derived {item_id}, which is no item")
            if item is not None and role == "stamp":
                check("ING-11", item.kind == Kind.RASTER, f"stamped a {item.kind}")

    # -- the end -----------------------------------------------------------------------------

    def teardown(self) -> None:
        if not hasattr(self, "store") or self.interrupted:  # a step failed before the end
            return
        self._drain()
        self._check_every_derivative_settled()
        before = self._snapshot()
        with self._connect() as conn:
            for item_id in self.cataloged:
                outbox.request_derive(conn, item_id, now=self.clock.now())
        for body in self.strays:
            self.queue.send(body)
        self._drain()
        check("ING-12", self._snapshot() == before, "redelivering every request changed state")
        self._check_rebuild_matches()

    def _drain(self) -> None:
        """Faults off; relay everything, run the stage until the queue is empty, then sweep the
        dead letters, as the worker does on a timer."""
        self.store.faults = self.queue.faults = Faults.none()
        for _ in range(MAX_DRAIN_ROUNDS):
            with self._connect() as conn:
                if outbox.relay(conn, self.queue, limit=100, now=self.clock.now()) == 0:
                    break
        for _ in range(MAX_DRAIN_ROUNDS):
            self.clock.advance(61)
            if len(self.queue) == 0:
                break
            self.work(batch=3)
        check("ING-12", len(self.queue) == 0, "the queue did not drain with faults off")
        for _ in range(MAX_DRAIN_ROUNDS):
            if sweep_derive_once(self.queue, self._connect, self.deriver, batch=3) == 0:
                break
        check("ING-12", self.queue.dead_letters == [], "dead letters left after the sweep")

    def _check_every_derivative_settled(self) -> None:
        jobs = self._jobs()
        for item in self.cataloged.values():
            for recipe in self.recipes:
                if recipe.role == "stamp" and item.kind != Kind.RASTER:
                    continue
                key = (str(item.id), recipe.role, recipe.version)
                stage, reason = jobs.get(key, ("missing", None))
                check("ING-12", stage in ("derived", "failed"), f"{key}: {stage}")
                refused = recipe.role == "picky" and item.content.startswith(b"bad")
                if stage == "failed" and reason not in TRANSIENT:
                    check("ING-12", refused, f"{key} failed as {reason}")
                if refused and reason not in TRANSIENT:
                    check("ING-12", (stage, reason) == ("failed", "unrenderable"), f"{key}")

    def _snapshot(self):
        storage = {n: self.store.content(DERIVED, n) for n in self.store.names(DERIVED)}
        return self._jobs(), self._assets(), storage

    def _equivalent(self) -> dict[Key, tuple[Any, ...]]:
        """Each derivative's outcome, and what a rebuild must reproduce of its asset: media
        type, lineage, and its checksum if its recipe is deterministic (ING-7)."""
        assets = self._assets()
        out: dict[Key, tuple[Any, ...]] = {}
        for key, (stage, reason) in self._jobs().items():
            asset = assets.get(key)
            if asset is None:
                out[key] = (stage, reason)
                continue
            media_type, _, size, sha, lineage = asset
            exact = (size, sha) if self._recipe(key[1], key[2]).deterministic else None
            out[key] = (stage, reason, media_type, lineage, exact)
        return out

    def _check_rebuild_matches(self) -> None:
        before = self._equivalent()
        for name in self.store.names(DERIVED):
            self.store.delete(DERIVED, name)
        with self.db.transaction():
            self.db.execute("DELETE FROM catalog.assets")
            self.db.execute("DELETE FROM ingest.derive_jobs")
        with self._connect() as conn:
            for item_id in self.cataloged:
                outbox.request_derive(conn, item_id, now=self.clock.now())
        self._drain()
        after = self._equivalent()
        check("ING-7", set(after) == set(before), "a rebuild made different derivatives")
        for key, was in before.items():
            if was[:2] in {("failed", reason) for reason in TRANSIENT}:
                continue  # the first run gave up on it; the rebuild had no faults
            check("ING-7", after[key] == was, f"{key} rebuilt as {after[key]}, was {was}")


@pytest.mark.invariant("ING-7", "ING-11", "ING-12")
def test_derive_simulation(fresh_database, tmp_path):
    url = fresh_database()
    with psycopg.connect(url, autocommit=True) as db:
        # Hypothesis accepts any factory here; its annotation only says `type`.
        run_state_machine_as_test(lambda: DeriveMachine(db, url, tmp_path))  # pyright: ignore[reportArgumentType]
