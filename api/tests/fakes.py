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

from ingest_api.ports import BlobNotFound, BlobProps, Message, TransientStorageError


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


class SeededIds:
    def __init__(self, rng: random.Random):
        self._rng = rng

    def new(self) -> str:
        return f"{self._rng.getrandbits(128):032x}"


@dataclass
class Faults:
    """Probabilities, each drawn from the owner's seeded RNG."""

    transient: float = 0.0  # a storage call fails with TransientStorageError
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

    def put(self, container, name, data, *, content_type=None, metadata=None) -> BlobProps:
        self._maybe_fail()
        self._writes += 1
        etag = hashlib.sha256(f"{self._writes}:{container}/{name}".encode()).hexdigest()[:16]
        props = BlobProps(container, name, len(data), etag, content_type, dict(metadata or {}))
        self._blobs[(container, name)] = (props, bytes(data))
        return props

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

    def delete(self, container, name) -> None:
        self._maybe_fail()
        self._blobs.pop((container, name), None)

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
    move to `dead_letters`. Faults add duplicates, reordering and lost completions, the
    behaviours at-least-once consumers have to survive.
    """

    clock: SimClock
    rng: random.Random
    faults: Faults = field(default_factory=Faults.none)
    lock_seconds: float = 60.0
    max_delivery_count: int = 10
    _entries: list[_Entry] = field(default_factory=list)
    dead_letters: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"m{self._seq}"

    def send(self, body: dict[str, Any]) -> None:
        copies = 2 if self.faults.duplicate and self.rng.random() < self.faults.duplicate else 1
        for _ in range(copies):
            self._entries.append(_Entry(self._next_id(), dict(body)))

    def _available(self) -> list[_Entry]:
        now = self.clock.monotonic()
        return [e for e in self._entries if e.locked_until is None or e.locked_until <= now]

    def receive(self, max_messages: int = 1) -> list[Message]:
        ready = self._available()
        if self.faults.reorder and self.rng.random() < self.faults.reorder:
            self.rng.shuffle(ready)
        out = []
        for entry in ready:
            if len(out) == max_messages:
                break
            if entry.delivery_count >= self.max_delivery_count:
                self._entries.remove(entry)
                self.dead_letters.append(entry.body)
                continue
            entry.delivery_count += 1
            entry.locked_until = self.clock.monotonic() + self.lock_seconds
            entry.lock_token = f"{entry.id}:{entry.delivery_count}"
            out.append(Message(entry.id, dict(entry.body), entry.delivery_count, entry.lock_token))
        return out

    def _locked(self, message: Message) -> _Entry | None:
        now = self.clock.monotonic()
        for entry in self._entries:
            if (
                entry.lock_token == message.lock_token
                and entry.locked_until is not None
                and entry.locked_until > now
            ):
                return entry
        return None

    def complete(self, message: Message) -> None:
        entry = self._locked(message)
        if entry is None:
            return  # lock lost: the message will be redelivered
        if self.faults.lost_complete and self.rng.random() < self.faults.lost_complete:
            return
        self._entries.remove(entry)

    def abandon(self, message: Message) -> None:
        entry = self._locked(message)
        if entry is not None:
            entry.locked_until = None

    def __len__(self) -> int:
        return len(self._entries)
