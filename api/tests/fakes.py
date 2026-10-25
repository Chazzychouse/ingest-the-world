"""Deterministic, fault-injecting stand-ins for the ports in `ingest_api.ports`.

Every source of randomness is a `random.Random` the caller seeds, and time only moves when
`SimClock.advance` is called, so a simulation run is replayable from its seed.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ingest_api.events import blob_created
from ingest_api.ports import (
    MAX_DELIVERIES_EXCEEDED,
    BlobChanged,
    BlobExists,
    BlobNotFound,
    BlobProps,
    DeadLetter,
    LockLost,
    Message,
    TransientStorageError,
)
from ingest_api.worker.extract import Budget, Failed, Found, Reader, read_header


def event_for(props: BlobProps) -> dict[str, Any]:
    """The BlobCreated event Event Grid would send for `props`; one per version, so its id is
    the etag."""
    return blob_created(
        props, account_url="http://sim", event_id=props.etag, event_time=SimClock().now()
    )


class SimClock:
    def __init__(self, start: datetime = datetime(2026, 1, 1, tzinfo=UTC)):
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class SeededIds:
    def __init__(self, rng: random.Random):
        self._rng = rng

    def new(self) -> str:
        return f"{self._rng.getrandbits(128):032x}"


@dataclass
class Faults:
    """Probabilities, each drawn from the owner's seeded RNG."""

    transient: float = 0.0  # a storage call fails with TransientStorageError
    lost_ack: float = 0.0  # a storage write takes effect, then reports TransientStorageError
    duplicate: float = 0.0  # a sent message is enqueued twice
    reorder: float = 0.0  # a receive returns messages out of order
    lost_complete: float = 0.0  # a completion is acknowledged but not applied

    @classmethod
    def none(cls) -> Faults:
        return cls()


class MemoryBlobStore:
    """In-memory `BlobStore` that counts bytes read per blob (ING-6) and can fail on cue."""

    def __init__(self, rng: random.Random | None = None, faults: Faults | None = None):
        self._rng = rng or random.Random(0)
        self.faults = faults or Faults.none()
        self._blobs: dict[tuple[str, str], tuple[BlobProps, bytes]] = {}
        self._writes = 0
        self.bytes_read: dict[tuple[str, str], int] = {}

    def _maybe_fail(self) -> None:
        if self.faults.transient and self._rng.random() < self.faults.transient:
            raise TransientStorageError("injected")

    def _maybe_lose_ack(self) -> None:
        if self.faults.lost_ack and self._rng.random() < self.faults.lost_ack:
            raise TransientStorageError("injected after the write took effect")

    def _write(self, container, name, data, content_type, metadata) -> BlobProps:
        self._writes += 1
        etag = hashlib.sha256(f"{self._writes}:{container}/{name}".encode()).hexdigest()[:16]
        props = BlobProps(container, name, len(data), etag, content_type, dict(metadata or {}))
        self._blobs[(container, name)] = (props, bytes(data))
        return props

    def put(self, container, name, data, *, content_type=None, metadata=None) -> BlobProps:
        self._maybe_fail()
        return self._write(container, name, data, content_type, metadata)

    def content(self, container: str, name: str) -> bytes:
        """The blob's bytes, for checks; not part of the port and never faulty or counted."""
        return self._blobs[(container, name)][1]

    def names(self, container: str) -> list[str]:
        """Every blob in `container`, for checks; not part of the port and never faulty."""
        return sorted(n for c, n in self._blobs if c == container)

    def head(self, container, name) -> BlobProps | None:
        self._maybe_fail()
        entry = self._blobs.get((container, name))
        return replace(entry[0]) if entry else None

    def read_range(self, container, name, offset, length) -> bytes:
        self._maybe_fail()
        entry = self._blobs.get((container, name))
        if entry is None:
            raise BlobNotFound(f"{container}/{name}")
        chunk = entry[1][offset : offset + max(length, 0)]
        key = (container, name)
        self.bytes_read[key] = self.bytes_read.get(key, 0) + len(chunk)
        return chunk

    def delete(self, container, name, *, if_match=None) -> None:
        self._maybe_fail()
        entry = self._blobs.get((container, name))
        if entry is None:
            return
        if if_match is not None and entry[0].etag != if_match:
            raise BlobChanged(f"{container}/{name} is not {if_match}")
        del self._blobs[(container, name)]
        self._maybe_lose_ack()

    def copy(self, source, destination, *, if_match, metadata=None) -> BlobProps:
        self._maybe_fail()
        entry = self._blobs.get(source)
        if entry is None or entry[0].etag != if_match:
            raise BlobChanged(f"{source} is not {if_match}")
        if destination in self._blobs:
            raise BlobExists(f"{destination}")
        props, data = entry
        container, name = destination
        copied = self._write(container, name, data, props.content_type, metadata or props.metadata)
        self._maybe_lose_ack()
        return copied

    def list(self, container, prefix="") -> Iterator[BlobProps]:
        self._maybe_fail()
        for (c, n), (props, _) in sorted(self._blobs.items()):
            if c == container and n.startswith(prefix):
                yield props


@dataclass
class _Entry:
    id: str
    body: dict[str, Any]
    delivery_count: int = 0
    locked_until: float | None = None
    lock_token: str | None = None


@dataclass
class SimQueue:
    """Peek-lock queue with Service Bus semantics, driven by a `SimClock`.

    Messages whose lock expires are redelivered; after `max_delivery_count` deliveries they
    move to the dead-letter queue, which is itself peek-lock. With `max_lock_renewal_seconds`
    set, a received message's lock is renewed (as the adapter's lock renewer does) until it is
    settled or that long has passed since it was received. Faults add duplicates, reordering and
    lost completions, the behaviours at-least-once consumers have to survive.
    """

    clock: SimClock
    rng: random.Random
    faults: Faults = field(default_factory=Faults.none)
    lock_seconds: float = 60.0
    max_delivery_count: int = 10
    max_lock_renewal_seconds: float = 0.0
    _entries: list[_Entry] = field(default_factory=list[_Entry])
    _dead: list[tuple[_Entry, str]] = field(default_factory=list[tuple[_Entry, str]])
    _seq: int = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"m{self._seq}"

    @property
    def dead_letters(self) -> list[DeadLetter]:
        return [DeadLetter(e.id, e.body, reason) for e, reason in self._dead]

    def send(self, body: dict[str, Any]) -> None:
        copies = 2 if self.faults.duplicate and self.rng.random() < self.faults.duplicate else 1
        for _ in range(copies):
            self._entries.append(_Entry(self._next_id(), dict(body)))

    def _ready(self, entries: list[_Entry]) -> list[_Entry]:
        now = self.clock.monotonic()
        return [e for e in entries if e.locked_until is None or e.locked_until <= now]

    def _lock(self, entry: _Entry, prefix: str = "") -> Message:
        entry.delivery_count += 1
        lock = self.lock_seconds + self.max_lock_renewal_seconds
        entry.locked_until = self.clock.monotonic() + lock
        entry.lock_token = f"{prefix}{entry.id}:{entry.delivery_count}"
        return Message(entry.id, dict(entry.body), entry.delivery_count, entry.lock_token)

    def receive(self, max_messages: int = 1) -> list[Message]:
        ready = self._ready(self._entries)
        if self.faults.reorder and self.rng.random() < self.faults.reorder:
            self.rng.shuffle(ready)
        out = []
        for entry in ready:
            if len(out) == max_messages:
                break
            if entry.delivery_count >= self.max_delivery_count:
                self._move_to_dead_letters(entry, MAX_DELIVERIES_EXCEEDED)
                continue
            out.append(self._lock(entry))
        return out

    def receive_dead_letters(self, max_messages: int = 1) -> list[Message]:
        ready = self._ready([e for e, _ in self._dead])[:max_messages]
        return [self._lock(entry, "dead:") for entry in ready]

    def _locked(self, message: Message) -> _Entry:
        now = self.clock.monotonic()
        for entry in [*self._entries, *(e for e, _ in self._dead)]:
            if (
                entry.lock_token == message.lock_token
                and entry.locked_until is not None
                and entry.locked_until > now
            ):
                return entry
        raise LockLost(message.id)

    def _move_to_dead_letters(self, entry: _Entry, reason: str) -> None:
        self._entries.remove(entry)
        entry.locked_until = None
        self._dead.append((entry, reason))

    def _remove(self, entry: _Entry) -> None:
        if entry in self._entries:
            self._entries.remove(entry)
            return
        self._dead = [(e, r) for e, r in self._dead if e is not entry]

    def complete(self, message: Message) -> None:
        entry = self._locked(message)
        if self.faults.lost_complete and self.rng.random() < self.faults.lost_complete:
            return
        self._remove(entry)

    def abandon(self, message: Message) -> None:
        self._locked(message).locked_until = None

    def dead_letter(self, message: Message, *, reason: str) -> None:
        entry = self._locked(message)
        if entry not in self._entries:
            raise LockLost(f"{message.id} is a dead letter already")
        self._move_to_dead_letters(entry, reason)

    def peek_dead_letters(self, max_messages: int) -> list[DeadLetter]:
        return self.dead_letters[:max_messages]

    def __len__(self) -> int:
        return len(self._entries)


class CrashAfter:
    """A `BlobStore` whose `calls`-th call takes effect and then fails, as if the worker died
    before hearing back. Every later call works. Lets a test interrupt a job after each step."""

    def __init__(self, store: MemoryBlobStore, calls: int):
        self._store = store
        self._remaining = calls
        self.crashed = False

    def _after(self, result):
        self._remaining -= 1
        if self._remaining == 0:
            self.crashed = True
            raise TransientStorageError("injected crash after the call took effect")
        return result

    def put(self, container, name, data, *, content_type=None, metadata=None) -> BlobProps:
        props = self._store.put(container, name, data, content_type=content_type, metadata=metadata)
        return self._after(props)

    def head(self, container, name) -> BlobProps | None:
        return self._after(self._store.head(container, name))

    def read_range(self, container, name, offset, length) -> bytes:
        return self._after(self._store.read_range(container, name, offset, length))

    def delete(self, container, name, *, if_match=None) -> None:
        self._after(self._store.delete(container, name, if_match=if_match))

    def copy(self, source, destination, *, if_match, metadata=None) -> BlobProps:
        return self._after(
            self._store.copy(source, destination, if_match=if_match, metadata=metadata)
        )

    def list(self, container, prefix="") -> Iterator[BlobProps]:
        return self._after(self._store.list(container, prefix))


class InlineSandbox:
    """Runs readers in this process: a reader that raises unexpectedly fails the test instead of
    becoming a `failed` extraction, which is what the format tests want."""

    def run(self, reader: Reader, budget: Budget) -> Found | Failed | None:
        return read_header(reader, budget.fetch, budget.name, budget.size)
