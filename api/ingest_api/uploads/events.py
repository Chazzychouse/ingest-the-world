"""Live status of the caller's own uploads, as server-sent events (`GET /uploads/events`).

A stream watches the caller's newest MAX_WATCHED uploads. It opens with each of them as it is,
then sends an upload again whenever anything the API says about it changes (its status, stage,
attempts, reason or signature), and picks up uploads the caller starts meanwhile. It asks the
database once a poll, on a connection held for that query alone, and ends after a fixed number of
polls; the client then opens another. Each stream starts from a fresh snapshot, so a reconnect
needs no `Last-Event-ID`: the stream carries state, not a log, and a change made while none was
open is folded into the next snapshot, never lost.

What a stream shows is what `GET /uploads` would show at that moment, read by the same query
(ACC-1); opening it is audited once (ACC-5).
"""

from collections.abc import AsyncIterator, Callable, Sequence
from uuid import UUID

import anyio.to_thread

from ingest_api.ports import Pause
from ingest_api.uploads.models import Upload

# The newest uploads a stream follows; older ones are settled, or the upload page lists them.
MAX_WATCHED = 100
# A comment line every this many quiet polls keeps proxies from closing an idle stream.
KEEPALIVE_POLLS = 10
# How long a client waits before reopening a stream that broke, in milliseconds.
RETRY_MS = 3000
EVENT_STREAM = "text/event-stream"


def event(name: str, data: str) -> bytes:
    """One server-sent event. `data` is one line of JSON, which never holds a raw newline."""
    return f"event: {name}\ndata: {data}\n\n".encode()


async def stream(
    watch: Callable[[], Sequence[Upload]], *, polls: int, interval: float, pause: Pause
) -> AsyncIterator[bytes]:
    """The events for `polls` polls of `watch`, `interval` seconds apart: each upload when first
    seen, and again whenever it changes. `watch` runs in a worker thread, as it blocks."""
    yield f"retry: {RETRY_MS}\n\n".encode()
    sent: dict[UUID, Upload] = {}
    for poll in range(polls):  # bounded: the stream's lifetime, counted in polls
        uploads = await anyio.to_thread.run_sync(watch)
        changed = [upload for upload in uploads if sent.get(upload.id) != upload]
        for upload in changed:
            yield event("upload", upload.model_dump_json())
        if not changed and poll % KEEPALIVE_POLLS == 0:
            yield b": keep-alive\n\n"
        # Only what is still watched: the record never outgrows MAX_WATCHED.
        sent = {upload.id: upload for upload in uploads}
        await pause(interval)
