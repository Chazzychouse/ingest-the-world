"""`Sandbox` as a child process per extraction, with a deadline and an address-space limit
(ING-8).

The child runs the reader and nothing else. It holds no credentials and no store: every block it
wants is a request over a pipe, which the worker serves through its `Budget`, so the budget and
storage failures are judged where the reader can't touch them (ING-6). A child that segfaults,
aborts, runs past its deadline, exceeds its memory or raises something unexpected is reported
as `Failed`, and the worker moves on.

Children come from a fork server, started once with the format libraries already imported, so
each extraction costs a fork rather than an interpreter start, and never forks a worker that has
threads (the queue's lock renewer).
"""

import logging
import multiprocessing
import resource
import signal
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
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

log = logging.getLogger("ingest.worker")

# Modules the fork server imports once, so children start with GDAL and Arrow loaded.
PRELOAD = ["ingest_api.worker.formats"]
# Requests one child may make: every block the budget allows, twice over (a reader that keeps
# asking after a refusal), plus its result. More means it's stuck in a loop: it is stopped.
MAX_REQUESTS = 2 * (MAX_EXTRACT_BYTES // BLOCK_BYTES) + 16
# How long a stopped child gets to exit before it is killed.
TERMINATE_GRACE_SECONDS = 1.0

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


class ProcessSandbox:
    def __init__(self, limits: Limits):
        self._limits = limits
        self._context = multiprocessing.get_context("forkserver")
        self._context.set_forkserver_preload(PRELOAD)

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


def _child(conn: Connection, reader: Reader, name: str, size: int, memory_bytes: int) -> None:
    """Runs in the child: the reader, over blocks fetched from the worker."""
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))

    def fetch(offset: int, length: int) -> bytes:
        conn.send(("read", offset, length))
        kind, payload = conn.recv()
        if kind == "data":
            return payload
        raise _ERRORS[payload](f"the worker refused bytes {offset}+{length}")

    try:
        conn.send(("found", read_header(reader, fetch, name, size)))
    except Exception as exc:  # noqa: BLE001 - whatever a reader raises, the worker hears why
        conn.send(("failed", f"{type(exc).__name__}: {exc}"[:500]))
