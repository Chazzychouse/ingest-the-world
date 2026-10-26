"""The derive stage: makes an item's derivatives from its original, one recipe at a time.

    derive request ──► item? ──for each recipe that applies──► job (item, role, version)
                         │                                     │
                    none: nothing        copy the original to scratch, render it in the
                                         renderer's sandbox ──create──► derived/
                                                               └──► catalog.assets   derived
                                     too large, or the recipe can't render it        failed

Each job ends `derived` or `failed` once, and every step is safe to repeat: a derivative's blob
name is fixed by its job, it is written with `create`, which never overwrites, a blob an earlier
attempt wrote is recorded instead of rendered again, and the asset row is written only once its
blob exists (ING-12). The stage reads items and writes only assets and its own jobs, so it never
changes an item, and only cataloged files are items (ING-11). One item is derived at a time,
under a lock on it, and its original is copied to scratch at most once for all its recipes.
"""

import hashlib
import tempfile
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache, partial
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow

from ingest_api.items import store as items
from ingest_api.ports import BlobExists, BlobProps, BlobStore, Clock, Container, StorageError
from ingest_api.worker import jobs
from ingest_api.worker.pipeline import Busy
from ingest_api.worker.recipe import CHUNK, Recipe, Renderer, Source, Unrenderable
from ingest_api.worker.recipes import RECIPES

DERIVED, RAW = Container.DERIVED, Container.RAW
# Blob metadata on a derivative: its checksum, so a retry can record a blob an earlier attempt
# wrote without reading it back, and the original's.
SHA256, SOURCE_SHA256 = "sha256", "source_sha256"
# Size classes S and M (spec section 4): originals up to this size are derived here, larger ones
# wait for M3's L class.
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_OUTPUT_BYTES = 2 * 1024**3
MAX_RECIPES = 32
# The original's copy in an item's scratch directory; each recipe renders in one beside it.
ORIGINAL = "original"


class Reason(StrEnum):
    """Why a derive job failed."""

    TOO_LARGE = "too-large"
    UNRENDERABLE = "unrenderable"
    RETRIES_EXHAUSTED = "retries-exhausted"
    DEAD_LETTERED = "dead-lettered"


@dataclass(frozen=True, slots=True)
class Deriver:
    store: BlobStore
    clock: Clock
    scratch: Path  # each item gets an empty directory of its own under here, removed after
    renderer: Renderer
    recipes: Sequence[Recipe] = field(default_factory=lambda: RECIPES)
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES


def blob_name(collection_id: str, item_id: UUID, recipe: Recipe) -> str:
    """Where a derivative lives in `derived/`: beside its item's other derivatives, under its
    collection as originals are in `raw/`, with one name per role and recipe version."""
    return f"{collection_id}/{item_id.hex}/{recipe.role}/v{recipe.version}{recipe.suffix}"


def derive(conn: Connection[TupleRow], d: Deriver, item_id: UUID) -> dict[str, jobs.DeriveStage]:
    """Brings every recipe that applies to the item to an outcome, or finds the one it has, by
    `role/vN`; empty if there is no such item. Raises Busy, StorageError or psycopg errors for the
    caller to retry; derivatives already made are kept."""
    with _locked(conn, item_id), tempfile.TemporaryDirectory(dir=d.scratch) as scratch:
        source = _source(conn, d, item_id)
        if source is None:
            return {}
        original = cache(partial(_download, source, Path(scratch) / ORIGINAL))
        return {_label(r): _run(conn, d, source, original, r) for r in _applying(d, source)}


def give_up(
    conn: Connection[TupleRow], d: Deriver, item_id: UUID, reason: Reason
) -> dict[str, jobs.DeriveStage]:
    """Ends every derivative of the item that has no outcome yet, so nothing is retried forever:
    one an earlier attempt wrote before it failed is kept, the rest fail with `reason`. Raises
    like `derive`."""
    with _locked(conn, item_id):
        source = _source(conn, d, item_id)
        if source is None:
            return {}
        stages: dict[str, jobs.DeriveStage] = {}
        for recipe in _applying(d, source):
            job = _begin(conn, d, source, recipe)
            stages[_label(recipe)] = job.stage
            if job.stage != "received":
                continue
            written = d.store.head(DERIVED, blob_name(source.collection_id, source.item_id, recipe))
            if written is None:
                stages[_label(recipe)] = _fail(conn, d, job, reason)
                continue
            stages[_label(recipe)] = _keep(conn, d, source, recipe, job, written)
        return stages


def _label(recipe: Recipe) -> str:
    return f"{recipe.role}/v{recipe.version}"


@contextmanager
def _locked(conn: Connection[TupleRow], item_id: UUID) -> Generator[None]:
    """Holds the lock on deriving one item for the block, and rolls back whatever the block left
    uncommitted on the way out."""
    key = jobs.derive_key(item_id)
    if not jobs.lock(conn, key):
        raise Busy(key)
    try:
        yield
    finally:
        conn.rollback()
        jobs.unlock(conn, key)
        conn.commit()


def _source(conn: Connection[TupleRow], d: Deriver, item_id: UUID) -> Source | None:
    original = items.original_of(conn, item_id)
    if original is None:
        return None
    fetch = partial(d.store.read_range, RAW, original.raw_blob)
    return Source(
        original.id,
        original.collection_id,
        original.kind,
        original.media_type,
        original.size_bytes,
        original.sha256,
        fetch,
        original.extraction,
        original.crs,
        original.properties,
    )


def _applying(d: Deriver, source: Source) -> list[Recipe]:
    return [recipe for recipe in d.recipes[:MAX_RECIPES] if recipe.applies(source)]


def _begin(
    conn: Connection[TupleRow], d: Deriver, source: Source, recipe: Recipe
) -> jobs.DeriveJobRow:
    job = jobs.begin_derive(conn, source.item_id, recipe.role, recipe.version, now=d.clock.now())
    conn.commit()
    return job


def _run(
    conn: Connection[TupleRow],
    d: Deriver,
    source: Source,
    original: Callable[[], Path],
    recipe: Recipe,
) -> jobs.DeriveStage:
    job = _begin(conn, d, source, recipe)
    if job.stage != "received":
        return job.stage  # a redelivery: the outcome stands (ING-12)
    if source.size > d.max_source_bytes:
        return _fail(conn, d, job, Reason.TOO_LARGE)
    name = blob_name(source.collection_id, source.item_id, recipe)
    try:
        blob = d.store.head(DERIVED, name) or _render(d, source, recipe, original(), name)
    except Unrenderable:
        return _fail(conn, d, job, Reason.UNRENDERABLE)
    return _keep(conn, d, source, recipe, job, blob)


def _keep(
    conn: Connection[TupleRow],
    d: Deriver,
    source: Source,
    recipe: Recipe,
    job: jobs.DeriveJobRow,
    blob: BlobProps,
) -> jobs.DeriveStage:
    """Records a written derivative as the job's asset, and the job as derived."""
    sha256 = blob.metadata.get(SHA256)
    if sha256 is None:
        raise StorageError(f"{DERIVED}/{blob.name} has no {SHA256}; it was not made by derive")
    asset = items.NewAsset(
        source.item_id,
        recipe.role,
        recipe.version,
        recipe.media_type,
        blob.name,
        blob.size,
        sha256,
        _lineage(source, recipe),
        recipe.title,
        recipe.stac_roles,
    )
    items.record_asset(conn, asset, created_at=d.clock.now())
    jobs.finish_derive(conn, job, "derived", reason=None, now=d.clock.now())
    conn.commit()
    return "derived"


def _fail(
    conn: Connection[TupleRow], d: Deriver, job: jobs.DeriveJobRow, reason: Reason
) -> jobs.DeriveStage:
    jobs.finish_derive(conn, job, "failed", reason=reason, now=d.clock.now())
    conn.commit()
    return "failed"


def _download(source: Source, path: Path) -> Path:
    """Copies the original to `path`, a chunk at a time, checking it is still what was
    cataloged."""
    digest = hashlib.sha256()
    with path.open("wb") as out:
        for chunk in source.chunks():
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != source.sha256:
        raise StorageError(f"the original of {source.item_id} no longer matches its checksum")
    return path


def _render(d: Deriver, source: Source, recipe: Recipe, original: Path, name: str) -> BlobProps:
    """Renders the derivative in a directory of its own beside the original's copy, and writes
    it to `derived/name`; if an earlier attempt got there first, its blob is the derivative."""
    prefix = f"{recipe.role}.v{recipe.version}."
    scratch = Path(tempfile.mkdtemp(prefix=prefix, dir=original.parent))
    path = d.renderer.render(recipe, original, scratch)
    # What a sandboxed render names is untrusted: it must be a file it wrote in its scratch.
    if not (path.resolve().is_relative_to(scratch.resolve()) and path.is_file()):
        raise Unrenderable(f"{recipe.role} named {path}, not a file in its scratch directory")
    size = path.stat().st_size
    if size > d.max_output_bytes:
        raise Unrenderable(f"{recipe.role} came to {size} bytes")
    metadata = {SHA256: _sha256(path, size), SOURCE_SHA256: source.sha256}
    try:
        with path.open("rb") as data:
            return d.store.create(
                DERIVED, name, data, content_type=recipe.media_type, metadata=metadata
            )
    except BlobExists:
        existing = d.store.head(DERIVED, name)
        if existing is None:
            raise StorageError(f"{DERIVED}/{name} was taken, then vanished") from None
        return existing


def _sha256(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for _ in range(0, size, CHUNK):
            digest.update(data.read(CHUNK))
    return digest.hexdigest()


def _lineage(source: Source, recipe: Recipe) -> dict[str, Any]:
    """STAC `processing:` fields for a derivative, and what it was made from. No timestamps, so
    a rebuild's lineage equals the original's (ING-7)."""
    return {
        "processing:lineage": f"{recipe.role} v{recipe.version} from the original",
        "processing:software": dict(recipe.software),
        "ingest:source_sha256": source.sha256,
    }
