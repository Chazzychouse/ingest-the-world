"""Local stand-in for Event Grid: announces committed `landing/` blobs on the ingest queue.

Azurite emits no events, so this polls the landing container and sends a BlobCreated event
(`ingest_api.events`) for every blob version it hasn't announced yet. Like Event Grid it
delivers at least once: a restart forgets what was sent and announces everything still in
landing again, which workers must absorb (ING-1). Uncommitted blocks are never listed, so
only finished uploads are announced, matching the cloud's commit-API filter.

    python -m ingest_api.event_shim
"""

import logging
from collections.abc import Mapping
from itertools import islice

from ingest_api.adapters.azure_blob import AzureBlobStore, ensure_containers, service_client
from ingest_api.adapters.service_bus import ServiceBusQueue
from ingest_api.events import blob_created
from ingest_api.ports import (
    BlobStore,
    Clock,
    Container,
    Ids,
    Queue,
    QueueError,
    StorageError,
    SystemClock,
    UuidIds,
)
from ingest_api.settings import Settings

log = logging.getLogger("ingest.event_shim")

# Blobs examined per poll. Workers move blobs out of landing, so a backlog beyond this is
# announced as earlier blobs leave.
MAX_BLOBS_PER_POLL = 5000

Announced = Mapping[str, str]  # landing blob name -> etag of the version already announced


def announce(
    store: BlobStore,
    queue: Queue,
    announced: Announced,
    *,
    account_url: str,
    clock: Clock,
    ids: Ids,
) -> dict[str, str]:
    """Sends an event for each landing blob whose current version isn't in `announced`, and
    returns what is announced now. Names no longer in landing are dropped, which keeps the
    table as small as the container. If a call fails the whole poll raises, and the next one
    resends everything: duplicates, never gaps."""
    now_announced: dict[str, str] = {}
    for blob in islice(store.list(Container.LANDING), MAX_BLOBS_PER_POLL):
        if announced.get(blob.name) != blob.etag:
            event = blob_created(
                blob, account_url=account_url, event_id=ids.new(), event_time=clock.now()
            )
            queue.send(event)
            log.info("announced %s (%s)", blob.name, blob.etag)
        now_announced[blob.name] = blob.etag
    return now_announced


def run(settings: Settings, clock: Clock, ids: Ids) -> None:
    key = settings.blob_account_key.get_secret_value() if settings.blob_account_key else None
    service = service_client(settings.blob_account_url, key)
    ensure_containers(service, Container)
    store = AzureBlobStore(service)
    connection = settings.queue_connection
    with ServiceBusQueue.connect(
        settings.queue_name,
        connection_string=connection.get_secret_value() if connection else None,
        namespace=settings.queue_namespace,
        max_wait_seconds=settings.queue_max_wait_seconds,
    ) as queue:
        log.info("watching %s/%s", settings.blob_account_url, Container.LANDING)
        announced: Announced = {}
        while True:  # unbounded by design: a daemon that runs until its container stops
            try:
                announced = announce(
                    store,
                    queue,
                    announced,
                    account_url=settings.blob_account_url,
                    clock=clock,
                    ids=ids,
                )
            except (StorageError, QueueError) as exc:
                log.warning("poll failed, retrying: %s", exc)
            clock.sleep(settings.shim_poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # it logs every HTTP request at INFO
    run(Settings(), SystemClock(), UuidIds())
