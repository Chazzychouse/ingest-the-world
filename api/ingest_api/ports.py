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
from enum import StrEnum
from typing import Any, Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class Ids(Protocol):
    def new(self) -> str: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class UuidIds:
    def new(self) -> str:
        return str(uuid.uuid4())


class Container(StrEnum):
    """The storage account's containers (`infra/terraform/storage.tf`); see spec section 3."""

    LANDING = "landing"
    RAW = "raw"
    DERIVED = "derived"
    QUARANTINE = "quarantine"


class StorageError(Exception):
    """Base for blob storage failures."""


class BlobNotFound(StorageError):
    pass


class TransientStorageError(StorageError):
    """Worth retrying: throttling, timeouts, 5xx."""


class BlobChanged(StorageError):
    """A conditional call's blob is missing or no longer the version (etag) it named."""


class BlobExists(StorageError):
    """A copy refused to overwrite its destination."""


@dataclass(frozen=True)
class BlobProps:
    container: str
    name: str
    size: int
    etag: str
    content_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict[str, str])
    # A server-side copy into this blob is still running; its content isn't final yet.
    copy_pending: bool = False


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

    def delete(self, container: str, name: str, *, if_match: str | None = None) -> None:
        """Removes the blob. Deleting a missing blob is not an error.

        With `if_match`, only that version is removed; a newer one raises BlobChanged.
        """
        ...

    def copy(
        self,
        source: tuple[str, str],
        destination: tuple[str, str],
        *,
        if_match: str,
        metadata: dict[str, str] | None = None,
    ) -> BlobProps:
        """Copies version `if_match` of the `(container, name)` source, server side, to a
        destination that must not exist yet, and waits for it to finish.

        Raises BlobChanged if the source isn't that version and BlobExists if the destination
        is taken.
        """
        ...

    def list(self, container: str, prefix: str = "") -> Iterator[BlobProps]:
        """Blobs under `prefix`, in name order."""
        ...


class BlobSigner(Protocol):
    """Issues signed URLs that let a browser move bytes without the API in the path (ACC-8)."""

    def upload_url(self, container: str, name: str, *, expires_at: datetime) -> str:
        """A URL that lets its holder create and write this one blob until `expires_at`, and
        do nothing else: no reads, listing, deletes, or other blobs."""
        ...


class QueueError(Exception):
    """Base for queue failures."""


class TransientQueueError(QueueError):
    """Worth retrying: connection loss, timeouts, a busy broker."""


class LockLost(QueueError):
    """The message's lock expired (or it was settled) before this settlement. It has been, or
    will be, redelivered to someone else, so the work must be safe to repeat (ING-1)."""


# The dead-letter reason Service Bus records when a message exhausts its deliveries.
MAX_DELIVERIES_EXCEEDED = "MaxDeliveryCountExceeded"


@dataclass(frozen=True, slots=True)
class ScanResult:
    clean: bool
    signature: str | None = None  # what was found, when not clean


class Scanner(Protocol):
    """Malware scanning: Defender for Storage in the cloud, a stand-in locally."""

    def scan(self, container: str, name: str) -> ScanResult: ...


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    body: dict[str, Any]
    delivery_count: int
    lock_token: str


@dataclass(frozen=True, slots=True)
class DeadLetter:
    id: str
    body: dict[str, Any]
    reason: str


class Queue(Protocol):
    """At-least-once, peek-lock delivery, as Service Bus queues provide.

    A received message is hidden until completed, abandoned, or its lock expires; then it
    is redelivered with a higher `delivery_count`. After the queue's maximum deliveries it moves
    to the dead-letter queue instead. Consumers must be idempotent (ING-1).

    Settling a message whose lock has expired raises `LockLost`.
    """

    def send(self, body: dict[str, Any]) -> None: ...

    def receive(self, max_messages: int = 1) -> list[Message]:
        """Up to `max_messages` messages, waiting briefly if none are ready; may be empty."""
        ...

    def complete(self, message: Message) -> None: ...

    def abandon(self, message: Message) -> None:
        """Releases the lock so the message is redelivered now."""
        ...

    def dead_letter(self, message: Message, *, reason: str) -> None:
        """Parks a message that can never succeed, so it stops being redelivered."""
        ...

    def peek_dead_letters(self, max_messages: int) -> list[DeadLetter]:
        """The oldest dead-lettered messages, without removing them."""
        ...
