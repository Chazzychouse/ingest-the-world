"""Readers can't take the worker down (ING-8), nor get round the budget from inside the sandbox
(ING-6).

The readers here misbehave on purpose: they crash the process natively, hang, exhaust memory,
raise what no reader should, or keep fetching after the budget says no. Each is a module-level
function so the sandbox's child process can import it by name.
"""

import ctypes
import os
import resource
import time
from functools import partial

import pytest

from ingest_api.adapters.process_sandbox import Limits, ProcessSandbox
from ingest_api.ports import Container, StorageError
from ingest_api.worker.classify import Media
from ingest_api.worker.extract import (
    BLOCK_BYTES,
    MAX_EXTRACT_BYTES,
    BlobSource,
    Budget,
    Found,
    Sandbox,
    extract,
)
from ingest_api.worker.formats import READERS
from tests.fakes import Faults, InlineSandbox, MemoryBlobStore
from tests.invariants import check
from tests.test_extract import geotiff

RAW = Container.RAW
BIG = 3 * MAX_EXTRACT_BYTES


def segfaults(source: BlobSource) -> Found:
    source.read(0, 16)
    ctypes.string_at(0)  # reads address 0: SIGSEGV, as a GDAL driver bug would
    return Found()


def aborts(_: BlobSource) -> Found:
    os.abort()


def hangs(_: BlobSource) -> Found:
    time.sleep(3600)
    return Found()


def hogs_memory(_: BlobSource) -> Found:
    hoard = [bytearray(64 * 1024 * 1024) for _ in range(1024)]  # 64 GiB, far past the limit
    return Found(properties={"blocks": len(hoard)})


def reports_core_limit(_: BlobSource) -> Found:
    soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
    return Found(properties={"core_soft": soft, "core_hard": hard})


def raises_a_bug(_: BlobSource) -> Found:
    raise KeyError("a reader bug")


def ignores_refusals(source: BlobSource) -> Found:
    """Asks for every block of the file, carrying on whatever the budget answers."""
    for offset in range(0, source.size, BLOCK_BYTES):
        try:
            source.read(offset, BLOCK_BYTES)
        except Exception:  # noqa: BLE001, S112 - the point is to ignore every refusal
            continue
    return Found(properties={"read": "everything"})


@pytest.fixture
def quick() -> ProcessSandbox:
    """Short limits, so the hanging and hoarding readers are stopped fast."""
    return ProcessSandbox(Limits(timeout_seconds=2.0, memory_bytes=512 * 1024 * 1024))


def _budget(store: MemoryBlobStore, data: bytes) -> Budget:
    props = store.put(RAW, "item", data)
    return Budget(partial(store.read_range, RAW, "item"), "item", props.size)


@pytest.mark.invariant("ING-8")
@pytest.mark.parametrize("reader", [segfaults, aborts, hangs, hogs_memory, raises_a_bug])
def test_a_reader_that_dies_fails_only_its_extraction(quick, reader):
    store = MemoryBlobStore()
    budget = _budget(store, b"II*\x00" + bytes(BIG))

    result = extract(reader, budget, quick)

    check("ING-8", result.metadata.status == "failed", f"{reader.__name__}: {result}")
    check("ING-8", result.kind is None and result.metadata.properties == {}, "kept metadata")
    # The sandbox is still usable afterwards: the next file is extracted as normal.
    after = extract(READERS[Media.GLB], _budget(store, b"glTF\x02"), quick)
    check("ING-8", after.metadata.status == "unreadable", f"next extraction: {after}")


@pytest.mark.invariant("ING-6")
def test_a_sandboxed_reader_cannot_fetch_past_the_budget(sandbox):
    store = MemoryBlobStore()
    budget = _budget(store, bytes(BIG))

    result = extract(ignores_refusals, budget, sandbox)

    fetched = store.bytes_read[(RAW, "item")]
    check("ING-6", fetched <= MAX_EXTRACT_BYTES, f"the sandbox fetched {fetched} bytes")
    check("ING-6", result.metadata.status == "over-budget", f"status {result.metadata.status}")
    check("ING-6", result.metadata.bytes_read == fetched, "bytes_read misreports the fetch")


@pytest.mark.parametrize("kind", ["inline", "process"])
def test_both_sandboxes_extract_a_geotiff_alike(tmp_path, sandbox, kind):
    chosen: Sandbox = sandbox if kind == "process" else InlineSandbox()
    store = MemoryBlobStore()
    budget = _budget(store, geotiff(tmp_path))

    result = extract(READERS[Media.TIFF], budget, chosen)
    reference = extract(
        READERS[Media.TIFF], _budget(MemoryBlobStore(), geotiff(tmp_path)), InlineSandbox()
    )
    assert result == reference
    assert result.metadata.status == "extracted"


def test_a_storage_failure_under_a_sandboxed_reader_is_retried(tmp_path, sandbox):
    store = MemoryBlobStore()
    budget = _budget(store, geotiff(tmp_path))
    store.faults = Faults(transient=1.0)

    with pytest.raises(StorageError):
        extract(READERS[Media.TIFF], budget, sandbox)


@pytest.mark.invariant("ING-8")
def test_a_reader_runs_where_a_crash_can_write_no_core_dump(quick):
    """A core file would hold the child's memory, and so the file's contents, on disk."""
    store = MemoryBlobStore()
    found = quick.run(reports_core_limit, _budget(store, b"x" * 16))

    assert isinstance(found, Found), found
    check("ING-8", found.properties == {"core_soft": 0, "core_hard": 0}, f"core limit {found}")
