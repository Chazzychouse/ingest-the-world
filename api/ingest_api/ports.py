"""Boundaries to everything the platform doesn't own: time, ids, blob storage, queues.

Production code takes these as parameters instead of reaching for `time`, `uuid` or an
SDK directly, so the simulator (`tests/sim/`) can swap in deterministic fakes that inject
faults. Each fake is held to the real adapter's behaviour by a shared contract suite
(`tests/contracts/`).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class Ids(Protocol):
    def new(self) -> str: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class UuidIds:
    def new(self) -> str:
        return str(uuid.uuid4())


class StorageError(Exception):
    """Base for blob storage failures."""


class BlobNotFound(StorageError):
    pass


class TransientStorageError(StorageError):
    """Worth retrying: throttling, timeouts, 5xx."""


@dataclass(frozen=True)
class BlobProps:
    container: str
    name: str
    size: int
    etag: str
    content_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class BlobStore(Protocol):
    def put(
        self,
        container: str,
        name: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BlobProps:
        """Creates or replaces a blob. Replacing always yields a new etag."""
        ...

    def head(self, container: str, name: str) -> BlobProps | None: ...

    def read_range(self, container: str, name: str, offset: int, length: int) -> bytes:
        """Up to `length` bytes from `offset`; fewer at the end, empty past it.

        Raises BlobNotFound for a missing blob.
        """
        ...

    def delete(self, container: str, name: str) -> None:
        """Removes the blob. Deleting a missing blob is not an error."""
        ...

    def list(self, container: str, prefix: str = "") -> Iterator[BlobProps]:
        """Blobs under `prefix`, in name order."""
        ...


@dataclass
class Message:
    id: str
    body: dict[str, Any]
    delivery_count: int
    lock_token: str


class Queue(Protocol):
    """At-least-once, peek-lock delivery, as Service Bus queues provide.

    A received message is hidden until completed, abandoned, or its lock expires; then it
    is redelivered with a higher `delivery_count`. Consumers must be idempotent (ING-1).
    """

    def send(self, body: dict[str, Any]) -> None: ...

    def receive(self, max_messages: int = 1) -> list[Message]: ...

    def complete(self, message: Message) -> None: ...

    def abandon(self, message: Message) -> None: ...
