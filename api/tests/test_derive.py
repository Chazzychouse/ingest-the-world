"""The derive stage, one item at a time, against Postgres and an in-memory blob store; and the
outbox relay that hands it cataloged items."""

import contextlib
import hashlib
import random
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from ingest_api.adapters.process_sandbox import ProcessRenderer
from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Metadata
from ingest_api.ports import Container, StorageError, TransientStorageError
from ingest_api.worker import derive, outbox
from ingest_api.worker.derive import SHA256, Deriver, Reason, blob_name
from ingest_api.worker.recipe import Recipe, Renderer, Source
from ingest_api.worker.recipes import RECIPES
from ingest_api.worker.recipes.raster import Cog
from ingest_api.worker.runner import Retry, derive_once, sweep_derive_once
from tests.conftest import TEST_LIMITS
from tests.fakes import INLINE_RENDERER, CrashAfter, MemoryBlobStore, SimClock, SimQueue
from tests.invariants import check
from tests.items import Cataloged, NewItem, catalog
from tests.recipes import Digest, DigestV2, Picky, Stamp
from tests.test_raster_recipes import metadata, rgb, write_raster

DERIVED, RAW = Container.DERIVED, Container.RAW
NOW = datetime(2026, 3, 1, tzinfo=UTC)
TIFF = b"II*\x00" + bytes(range(256)) * 4


class Counting:
    """A recipe that counts its renders."""

    def __init__(self, inner: Recipe):
        self.inner, self.renders = inner, 0
        self.role, self.version, self.media_type = inner.role, inner.version, inner.media_type
        self.title, self.stac_roles = inner.title, inner.stac_roles
        self.suffix, self.deterministic, self.software = (
            inner.suffix,
            inner.deterministic,
            inner.software,
        )

    def applies(self, source: Source) -> bool:
        return self.inner.applies(source)

    def render(self, original: Path, scratch: Path) -> Path:
        self.renders += 1
        return self.inner.render(original, scratch)


@pytest.fixture
def item(db, fresh_collection) -> Callable[..., Cataloged]:
    """Catalogs an item whose original is `content`, and puts that original in `store`."""

    def make(
        store: MemoryBlobStore,
        content: bytes = TIFF,
        kind: Kind = Kind.RASTER,
        *,
        collection: str | None = None,
        metadata: Metadata | None = None,
    ) -> Cataloged:
        new = NewItem(
            collection or fresh_collection,
            "f.tif",
            NOW,
            kind=kind,
            content=content,
            metadata=metadata,
        )
        cataloged = catalog(db, new)
        store.put(RAW, cataloged.raw_blob, content)
        return cataloged

    return make


@pytest.fixture
def run(database_url, tmp_path) -> Callable[..., dict[str, str]]:
    def _run(
        store, item_id: UUID, *, recipes, renderer: Renderer = INLINE_RENDERER, **limits
    ) -> dict[str, str]:
        deriver = Deriver(store, SimClock(NOW), tmp_path, renderer, recipes, **limits)
        with psycopg.connect(database_url) as conn:
            return dict(derive.derive(conn, deriver, item_id))

    return _run


def _assets(db, item_id: UUID) -> list[tuple[str, int, str, str, int, str]]:
    return db.execute(
        "SELECT role, recipe_version, media_type, blob_name, size_bytes, sha256 "
        "FROM catalog.assets WHERE item_id = %s ORDER BY role",
        (item_id,),
    ).fetchall()


def _jobs(db, item_id: UUID) -> list[tuple[str, str, str | None]]:
    return db.execute(
        "SELECT role, stage, reason FROM ingest.derive_jobs WHERE item_id = %s ORDER BY role",
        (item_id,),
    ).fetchall()


@pytest.mark.invariant("ING-12")
def test_each_recipe_that_applies_leaves_one_asset_and_its_blob(db, item, run):
    store = MemoryBlobStore()
    raster, table = item(store), item(store, b"a,b\n1,2\n", Kind.TABULAR)
    recipes = [Digest(), Stamp(random.Random(0))]

    assert run(store, raster.id, recipes=recipes) == {"digest/v1": "derived", "stamp/v1": "derived"}
    assert run(store, table.id, recipes=recipes) == {"digest/v1": "derived"}

    digest = hashlib.sha256(TIFF).hexdigest().encode()
    name = blob_name(raster.item.collection, raster.id, Digest())
    assert name == f"{raster.item.collection}/{raster.id.hex}/digest/v1.txt"
    assert store.content(DERIVED, name) == digest
    sha = hashlib.sha256(digest).hexdigest()
    [digest_asset, stamp_asset] = _assets(db, raster.id)
    assert digest_asset == ("digest", 1, "text/plain", name, len(digest), sha)
    assert stamp_asset[0] == "stamp"
    assert store.content(DERIVED, stamp_asset[3])[8:] == TIFF[:16]
    assert [role for role, *_ in _assets(db, table.id)] == ["digest"]
    lineage = db.execute(
        "SELECT lineage FROM catalog.assets WHERE blob_name = %s", (name,)
    ).fetchone()[0]
    assert lineage == {
        "processing:lineage": "digest v1 from the original",
        "processing:software": {"tests": "1"},
        "ingest:source_sha256": hashlib.sha256(TIFF).hexdigest(),
    }


@pytest.mark.invariant("ING-12")
def test_redelivering_a_request_renders_nothing_again(db, item, run):
    store, recipe = MemoryBlobStore(), Counting(Stamp(random.Random(0)))
    raster = item(store)
    run(store, raster.id, recipes=[recipe])
    before = (_assets(db, raster.id), store.names(DERIVED))

    assert run(store, raster.id, recipes=[recipe]) == {"stamp/v1": "derived"}
    assert recipe.renders == 1
    assert (_assets(db, raster.id), store.names(DERIVED)) == before


@pytest.mark.invariant("ING-11")
def test_a_request_for_anything_but_an_item_does_nothing(db, run, start):
    store = MemoryBlobStore()
    pending = start(10)  # an upload with no item, as a quarantined or rejected one has none

    for item_id in (pending.upload_id, uuid4()):
        assert run(store, item_id, recipes=[Digest()]) == {}
        assert _jobs(db, item_id) == []
    assert store.names(DERIVED) == []


@pytest.mark.invariant("ING-12")
def test_an_original_a_recipe_refuses_fails_that_derivative_only(db, item, run):
    store = MemoryBlobStore()
    bad = item(store, b"bad bytes")

    assert run(store, bad.id, recipes=[Picky(), Digest()]) == {
        "picky/v2": "failed",
        "digest/v1": "derived",
    }
    assert _jobs(db, bad.id) == [("digest", "derived", None), ("picky", "failed", "unrenderable")]
    assert [role for role, *_ in _assets(db, bad.id)] == ["digest"]
    assert store.names(DERIVED) == [blob_name(bad.item.collection, bad.id, Digest())]


class Escapes(Digest):
    """Names the original's copy as its derivative, as a compromised render might."""

    def render(self, original: Path, scratch: Path) -> Path:
        return original


@pytest.mark.invariant("ING-12")
def test_a_render_that_names_a_file_outside_its_scratch_fails(db, item, run):
    store = MemoryBlobStore()
    raster = item(store)

    assert run(store, raster.id, recipes=[Escapes()]) == {"digest/v1": "failed"}
    assert _jobs(db, raster.id) == [("digest", "failed", "unrenderable")]
    assert store.names(DERIVED) == []


@pytest.mark.invariant("ING-7")
def test_an_original_that_no_longer_matches_its_checksum_is_not_derived(db, item, run):
    """Lineage names the original by checksum; a derivative of other bytes would be a lie."""
    store = MemoryBlobStore()
    raster = item(store)
    store.put(RAW, raster.raw_blob, TIFF[::-1])

    with pytest.raises(StorageError):
        run(store, raster.id, recipes=[Digest()])
    check("ING-7", store.names(DERIVED) == [], "derived from bytes other than the original")
    assert _assets(db, raster.id) == []


@pytest.mark.invariant("ING-12")
def test_an_original_over_the_size_class_fails_without_rendering(db, item, run):
    store, recipe = MemoryBlobStore(), Counting(Digest())
    raster = item(store)

    assert run(store, raster.id, recipes=[recipe], max_source_bytes=len(TIFF) - 1) == {
        "digest/v1": "failed"
    }
    assert (recipe.renders, _jobs(db, raster.id)) == (0, [("digest", "failed", "too-large")])
    assert store.names(DERIVED) == []


@pytest.mark.invariant("ING-12")
def test_a_blob_an_earlier_attempt_wrote_is_recorded_not_rendered(db, item, run):
    store, recipe = MemoryBlobStore(), Counting(Digest())
    raster = item(store)
    name = blob_name(raster.item.collection, raster.id, recipe)
    earlier = store.put(DERIVED, name, b"earlier", metadata={SHA256: "e" * 64})

    assert run(store, raster.id, recipes=[recipe]) == {"digest/v1": "derived"}
    assert recipe.renders == 0
    assert _assets(db, raster.id) == [("digest", 1, "text/plain", name, earlier.size, "e" * 64)]


@pytest.mark.invariant("ING-12")
def test_a_blob_derive_did_not_write_is_never_recorded(db, item, run):
    store = MemoryBlobStore()
    raster = item(store)
    store.put(DERIVED, blob_name(raster.item.collection, raster.id, Digest()), b"someone else's")

    with pytest.raises(Exception, match="not made by derive"):
        run(store, raster.id, recipes=[Digest()])
    assert _assets(db, raster.id) == []
    assert _jobs(db, raster.id) == [("digest", "received", None)]


@pytest.mark.invariant("ING-12")
def test_a_job_interrupted_after_any_storage_call_finishes_the_same(db, item, run, new_collection):
    """Crashes the stage right after each storage call in turn, then lets the retry run. Each run
    catalogs into a collection of its own, so none is a duplicate of another (ING-9)."""
    recipes = [Digest(), Picky()]
    reference = MemoryBlobStore()
    ref = item(reference)
    run(reference, ref.id, recipes=recipes)

    def state(store: MemoryBlobStore, cataloged: Cataloged):
        assets = [(*a[:3], a[4:]) for a in _assets(db, cataloged.id)]
        prefix = f"{cataloged.item.collection}/{cataloged.id.hex}/"
        blobs = [n.replace(prefix, "") for n in store.names(DERIVED)]
        return assets, _jobs(db, cataloged.id), blobs

    expected = state(reference, ref)
    interrupted = 0
    for calls in range(1, 20):
        memory = MemoryBlobStore()
        cataloged = item(memory, collection=new_collection())
        crashing = CrashAfter(memory, calls)
        with contextlib.suppress(TransientStorageError):  # the injected crash
            run(crashing, cataloged.id, recipes=recipes)
        if not crashing.crashed:
            break
        interrupted += 1
        run(memory, cataloged.id, recipes=recipes)
        assert state(memory, cataloged) == expected
    assert interrupted >= 4, "the stage should take several storage calls"


@pytest.mark.invariant("ING-12")
def test_giving_up_fails_what_is_unfinished_and_keeps_what_is_made(
    db, item, database_url, tmp_path
):
    store = MemoryBlobStore()
    raster = item(store)
    deriver = Deriver(store, SimClock(NOW), tmp_path, INLINE_RENDERER, [Digest()])
    with psycopg.connect(database_url) as conn:
        derive.derive(conn, deriver, raster.id)
        more = Deriver(store, SimClock(NOW), tmp_path, INLINE_RENDERER, [Digest(), Picky()])
        stages = derive.give_up(conn, more, raster.id, Reason.RETRIES_EXHAUSTED)

    assert stages == {"digest/v1": "derived", "picky/v2": "failed"}
    assert _jobs(db, raster.id) == [
        ("digest", "derived", None),
        ("picky", "failed", "retries-exhausted"),
    ]


@pytest.mark.invariant("ING-12")
def test_giving_up_keeps_a_derivative_written_before_the_attempts_ran_out(
    db, item, database_url, tmp_path
):
    """A write that took effect but reported failure leaves a whole blob; failing its job would
    orphan it."""
    store = MemoryBlobStore()
    raster = item(store)
    deriver = Deriver(store, SimClock(NOW), tmp_path, INLINE_RENDERER, [Digest()])
    crashing = Deriver(CrashAfter(store, 3), SimClock(NOW), tmp_path, INLINE_RENDERER, [Digest()])
    with psycopg.connect(database_url) as conn:
        with pytest.raises(TransientStorageError):  # head, read, then create takes effect
            derive.derive(conn, crashing, raster.id)
        stages = derive.give_up(conn, deriver, raster.id, Reason.RETRIES_EXHAUSTED)

    assert stages == {"digest/v1": "derived"}
    assert _jobs(db, raster.id) == [("digest", "derived", None)]
    assert [a[3] for a in _assets(db, raster.id)] == store.names(DERIVED)


def _settled(db, item_id: UUID) -> bool:
    row = db.execute("SELECT 1 FROM ingest.derive_settled WHERE item_id = %s", (item_id,))
    return row.fetchone() is not None


@pytest.mark.invariant("ING-13")
def test_an_item_is_not_ready_while_a_new_recipe_derives_it(db, item, database_url, tmp_path):
    store = MemoryBlobStore()
    raster = item(store)
    shipped = [Digest(), DigestV2()]  # a new version, deployed after the item was derived
    first = Deriver(store, SimClock(NOW), tmp_path, INLINE_RENDERER, [Digest()])
    crashing = Deriver(CrashAfter(store, 1), SimClock(NOW), tmp_path, INLINE_RENDERER, shipped)
    after = Deriver(store, SimClock(NOW), tmp_path, INLINE_RENDERER, shipped)
    with psycopg.connect(database_url) as conn:
        derive.derive(conn, first, raster.id)
        check("ING-13", _settled(db, raster.id), "an item derived in full is not ready")
        with pytest.raises(TransientStorageError):
            derive.derive(conn, crashing, raster.id)
        check("ING-13", not _settled(db, raster.id), "ready while digest/v2 is in flight")
        derive.derive(conn, after, raster.id)

    check("ING-13", _settled(db, raster.id), "not ready once every recipe has an outcome")


# -- the outbox ---------------------------------------------------------------------------------


@pytest.fixture
def queue() -> SimQueue:
    return SimQueue(clock=SimClock(), rng=random.Random(0))


def _unpublished(db) -> int:
    return db.execute("SELECT count(*) FROM ingest.outbox WHERE published_at IS NULL").fetchone()[0]


def test_the_relay_sends_each_request_once_oldest_first(db, database_url, queue):
    db.execute("DELETE FROM ingest.outbox")
    ids = [uuid4() for _ in range(3)]
    with psycopg.connect(database_url) as conn:
        for item_id in ids:
            outbox.request_derive(conn, item_id, now=NOW)
        conn.commit()
        assert outbox.relay(conn, queue, limit=2, now=NOW) == 2
        assert outbox.relay(conn, queue, limit=2, now=NOW) == 1
        assert outbox.relay(conn, queue, limit=2, now=NOW) == 0

    sent = [outbox.parse_derive(m.body).item_id for m in queue.receive(10)]
    assert sent == ids
    assert _unpublished(db) == 0


class Down(SimQueue):
    def send(self, body):
        raise TransientStorageError("down")


def test_a_relay_that_fails_to_send_stamps_nothing(db, database_url):
    db.execute("DELETE FROM ingest.outbox")
    with psycopg.connect(database_url) as conn:
        outbox.request_derive(conn, uuid4(), now=NOW)
        conn.commit()
        with pytest.raises(TransientStorageError):
            outbox.relay(conn, Down(clock=SimClock(), rng=random.Random(0)), limit=10, now=NOW)
        conn.rollback()

    assert _unpublished(db) == 1


@pytest.mark.parametrize("body", [{}, {"item_id": 7}, {"item_id": "not-a-uuid"}])
def test_a_body_that_is_not_a_derive_request_is_refused(body):
    with pytest.raises(outbox.InvalidRequest):
        outbox.parse_derive(body)


# -- the runner ---------------------------------------------------------------------------------

RETRY = Retry(max_deliveries=3, backoff_seconds=1.0, max_backoff_seconds=4.0)


class ReadsFail(MemoryBlobStore):
    def read_range(self, container, name, offset, length):
        raise TransientStorageError("injected")


@pytest.fixture
def deriving(database_url, tmp_path):
    """Runs the derive queue as the worker does, until it takes no message."""

    def _deriving(queue: SimQueue, store: MemoryBlobStore, recipes) -> None:
        deriver = Deriver(store, queue.clock, tmp_path, INLINE_RENDERER, recipes)
        connect = lambda: psycopg.connect(database_url)  # noqa: E731 - a one-line factory
        for _ in range(10):
            if derive_once(queue, connect, deriver, retry=RETRY, batch=10) == 0:
                return

    return _deriving


def test_a_request_is_completed_and_a_malformed_one_dead_lettered(db, item, queue, deriving):
    store = MemoryBlobStore()
    raster = item(store)
    queue.send({"item_id": str(raster.id)})
    queue.send({"item_id": "nope"})

    deriving(queue, store, [Digest()])

    assert len(queue) == 0
    assert [d.body for d in queue.dead_letters] == [{"item_id": "nope"}]
    assert _jobs(db, raster.id) == [("digest", "derived", None)]


@pytest.mark.invariant("ING-12")
def test_a_request_still_failing_on_its_last_delivery_fails_its_derivatives(
    db, item, queue, deriving
):
    store = ReadsFail()
    raster = item(store)
    queue = SimQueue(clock=queue.clock, rng=random.Random(0), max_delivery_count=3)
    queue.send({"item_id": str(raster.id)})

    deriving(queue, store, [Digest()])

    assert _jobs(db, raster.id) == [("digest", "failed", "retries-exhausted")]
    assert len(queue) == 0 and queue.dead_letters == []
    attempts = db.execute(
        "SELECT attempts FROM ingest.derive_jobs WHERE item_id = %s", (raster.id,)
    )
    assert attempts.fetchone() == (4,)  # three deliveries, then giving up


@pytest.mark.invariant("ING-12")
def test_a_request_whose_message_dead_letters_is_failed_by_the_sweep(
    db, item, queue, database_url, tmp_path
):
    store = MemoryBlobStore()
    raster = item(store)
    queue.send({"item_id": str(raster.id)})
    for _ in range(queue.max_delivery_count):  # a worker that dies on every delivery
        queue.receive()
        queue.clock.advance(queue.lock_seconds + 1)
    assert queue.receive() == [] and len(queue.dead_letters) == 1
    deriver = Deriver(store, queue.clock, tmp_path, INLINE_RENDERER, [Digest()])

    connect = lambda: psycopg.connect(database_url)  # noqa: E731 - a one-line factory
    assert sweep_derive_once(queue, connect, deriver, batch=10) == 1
    assert _jobs(db, raster.id) == [("digest", "failed", "dead-lettered")]
    assert queue.dead_letters == []


# -- the raster recipes, rendered in the sandbox --------------------------------------------------


@pytest.fixture(scope="module")
def renderer() -> ProcessRenderer:
    return ProcessRenderer(TEST_LIMITS, max_file_bytes=derive.MAX_OUTPUT_BYTES)


@pytest.fixture
def raster(item, tmp_path) -> Callable[..., Cataloged]:
    """Catalogs a GeoTIFF with what the raster reader records about it."""

    def make(store: MemoryBlobStore, *, cog: bool = False) -> Cataloged:
        path = write_raster(tmp_path / f"{uuid4().hex}.tif", rgb(600, 800), nodata=0)
        if cog:
            path = Cog().render(path, Path(tempfile.mkdtemp(dir=tmp_path)))
        return item(store, path.read_bytes(), metadata=metadata(path))

    return make


@pytest.mark.invariant("ING-7")
@pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")
def test_a_geotiff_gets_a_cog_and_a_thumbnail_that_rebuild_the_same(db, raster, run, renderer):
    store = MemoryBlobStore()
    ortho = raster(store)

    assert run(store, ortho.id, recipes=RECIPES, renderer=renderer) == {
        "cog/v1": "derived",
        "thumbnail/v1": "derived",
    }
    built = _assets(db, ortho.id)
    assert [(role, media) for role, _, media, *_ in built] == [
        ("cog", Cog.media_type),
        ("thumbnail", "image/png"),
    ]
    for _, _, _, name, size, sha in built:
        assert hashlib.sha256(store.content(DERIVED, name)).hexdigest() == sha
        assert len(store.content(DERIVED, name)) == size

    for name in store.names(DERIVED):
        store.delete(DERIVED, name)
    db.execute("DELETE FROM catalog.assets WHERE item_id = %s", (ortho.id,))
    db.execute("DELETE FROM ingest.derive_jobs WHERE item_id = %s", (ortho.id,))
    run(store, ortho.id, recipes=RECIPES, renderer=renderer)

    check("ING-7", _assets(db, ortho.id) == built, "a rebuild differs from the first build")


def test_a_cog_original_gets_only_a_thumbnail(db, raster, run, renderer):
    store = MemoryBlobStore()
    cog = raster(store, cog=True)

    assert run(store, cog.id, recipes=RECIPES, renderer=renderer) == {"thumbnail/v1": "derived"}
