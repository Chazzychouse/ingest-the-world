"""`Sandbox` as a child process per extraction, and `Renderer` as one per render, each with a
deadline and an address-space limit (ING-8).

The child runs the reader and nothing else. It holds no credentials and no store: every block it
wants is a request over a pipe, which the worker serves through its `Budget`, so the budget and
storage failures are judged where the reader can't touch them (ING-6). A child that segfaults,
aborts, runs past its deadline, exceeds its memory or raises something unexpected is reported
as `Failed`, and the worker moves on.

A render's child gets a local copy of the original and an empty scratch directory, and nothing
else: no store, no credentials. It also may not write a file larger than a derivative may be.
Whatever goes wrong in it is `Unrenderable`, and the derive job fails rather than retrying.

Children come from a fork server, started once with the format libraries and recipes already
imported, so each job costs a fork rather than an interpreter start, and never forks a worker
that has threads (the queue's lock renewer).
"""

import logging
import multiprocessing
import resource
import signal
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.context import ForkServerContext
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Literal

from ingest_api.ports import StorageError
from ingest_api.worker.extract import (
    BLOCK_BYTES,
    MAX_EXTRACT_BYTES,
    Budget,
    BudgetExceeded,
    Failed,
    Found,
    Reader,
    Unreadable,
    read_header,
)
from ingest_api.worker.recipe import Recipe, Unrenderable

log = logging.getLogger("ingest.worker")

# Modules the fork server imports once, so children start with GDAL and Arrow loaded.
PRELOAD = ["ingest_api.worker.formats", "ingest_api.worker.recipes"]
# Requests one child may make: every block the budget allows, twice over (a reader that keeps
# asking after a refusal), plus its result. More means it's stuck in a loop: it is stopped.
MAX_REQUESTS = 2 * (MAX_EXTRACT_BYTES // BLOCK_BYTES) + 16
# How long a stopped child gets to exit before it is killed.
TERMINATE_GRACE_SECONDS = 1.0
# The longest error a child reports.
MAX_REASON = 500

type ErrorKind = Literal["storage", "budget", "unreadable"]
_ERRORS: dict[ErrorKind, type[Exception]] = {
    "storage": StorageError,
    "budget": BudgetExceeded,
    "unreadable": Unreadable,
}


class Exited:
    """The child went away without sending a result."""


EXITED = Exited()


@dataclass(frozen=True, slots=True)
class Limits:
    timeout_seconds: float
    memory_bytes: int


def _forkserver() -> ForkServerContext:
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(PRELOAD)
    return context


class ProcessSandbox:
    def __init__(self, limits: Limits):
        self._limits = limits
        self._context = _forkserver()

    def run(self, reader: Reader, budget: Budget) -> Found | Failed | None:
        parent, child = self._context.Pipe()
        process = self._context.Process(
            target=_child,
            args=(child, reader, budget.name, budget.size, self._limits.memory_bytes),
            daemon=True,
        )
        process.start()
        child.close()  # the child holds its end now; EOF on ours means it has gone
        try:
            result = _serve(
                parent, budget, deadline=time.monotonic() + self._limits.timeout_seconds
            )
        finally:
            parent.close()
            _stop(process)
        if isinstance(result, Exited):
            return Failed(_exit_reason(process.exitcode))
        return result


def _serve(conn: Connection, budget: Budget, *, deadline: float) -> Found | Failed | Exited | None:
    """Answers the child's block requests until it reports a result, dies or runs out of time."""
    for _ in range(MAX_REQUESTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not conn.poll(remaining):
            return Failed("timed out")
        try:
            message = conn.recv()
        except EOFError:
            return EXITED
        match message:
            case ("read", int(offset), int(length)):
                conn.send(_fetch(budget, offset, length))
            case ("found", found):
                return found if isinstance(found, Found) else None
            case ("failed", str(reason)):
                return Failed(reason)
            case _:
                return Failed(f"sent an unexpected message of type {type(message).__name__}")
    return Failed(f"made more than {MAX_REQUESTS} requests")


def _fetch(budget: Budget, offset: int, length: int) -> tuple[str, bytes] | tuple[str, ErrorKind]:
    try:
        return ("data", budget.fetch(offset, length))
    except StorageError:
        return ("error", "storage")
    except BudgetExceeded:
        return ("error", "budget")
    except Unreadable:
        return ("error", "unreadable")


def _stop(process: BaseProcess) -> None:
    process.join(TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()


def _exit_reason(exitcode: int | None) -> str:
    if exitcode is not None and exitcode < 0:
        return f"killed by {signal.Signals(-exitcode).name}"
    return f"exited with status {exitcode} and no result"


def _confine(memory_bytes: int) -> None:
    """Limits the child it runs in."""
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    # A crash must not write the child's memory, which holds file contents, to a core file.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _child(conn: Connection, reader: Reader, name: str, size: int, memory_bytes: int) -> None:
    """Runs in the child: the reader, over blocks fetched from the worker."""
    _confine(memory_bytes)

    def fetch(offset: int, length: int) -> bytes:
        conn.send(("read", offset, length))
        kind, payload = conn.recv()
        if kind == "data":
            return payload
        raise _ERRORS[payload](f"the worker refused bytes {offset}+{length}")

    try:
        conn.send(("found", read_header(reader, fetch, name, size)))
    except Exception as exc:  # noqa: BLE001 - whatever a reader raises, the worker hears why
        conn.send(("failed", f"{type(exc).__name__}: {exc}"[:MAX_REASON]))


class ProcessRenderer:
    def __init__(self, limits: Limits, *, max_file_bytes: int):
        self._limits = limits
        self._max_file_bytes = max_file_bytes
        self._context = _forkserver()

    def render(self, recipe: Recipe, original: Path, scratch: Path) -> Path:
        receiver, sender = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_render_child,
            args=(sender, recipe, original, scratch, self._limits, self._max_file_bytes),
            daemon=True,
        )
        process.start()
        sender.close()  # the child holds its end now; EOF on ours means it has gone
        try:
            result = _outcome(receiver, deadline=time.monotonic() + self._limits.timeout_seconds)
        finally:
            receiver.close()
            _stop(process)
        match result:
            case ("rendered", str(path)):
                return Path(path)
            case ("unrenderable", str(reason)):
                raise Unrenderable(reason)
            case Exited():
                raise Unrenderable(_exit_reason(process.exitcode))
            case _:
                raise Unrenderable(f"sent an unexpected message of type {type(result).__name__}")


def _outcome(conn: Connection, *, deadline: float) -> object:
    """The one message a render's child sends, or Exited; Unrenderable if it runs out of time."""
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not conn.poll(remaining):
        raise Unrenderable("timed out")
    try:
        return conn.recv()
    except EOFError:
        return EXITED


def _render_child(
    conn: Connection,
    recipe: Recipe,
    original: Path,
    scratch: Path,
    limits: Limits,
    max_file_bytes: int,
) -> None:
    """Runs in the child: the recipe, over the original's local copy."""
    _confine(limits.memory_bytes)
    # A write past this fails (Python ignores SIGXFSZ), so a runaway render stops at the size
    # the derive stage would refuse anyway.
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
    try:
        path = recipe.render(original, scratch)
    except Exception as exc:  # noqa: BLE001 - whatever a recipe raises, the worker hears why
        conn.send(("unrenderable", f"{type(exc).__name__}: {exc}"[:MAX_REASON]))
        return
    conn.send(("rendered", str(path)))
