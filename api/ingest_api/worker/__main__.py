"""The pipeline worker: `python -m ingest_api.worker`.

Locally it scans with the EICAR stand-in; Defender for Storage's verdicts replace it with the
cloud deployment.
"""

import logging

from psycopg_pool import ConnectionPool

from ingest_api.adapters.azure_blob import AzureBlobStore, ensure_containers, service_client
from ingest_api.adapters.eicar_scanner import EicarScanner
from ingest_api.adapters.service_bus import ServiceBusQueue
from ingest_api.ports import Container, QueueError, SystemClock
from ingest_api.settings import Settings
from ingest_api.worker.pipeline import Pipeline
from ingest_api.worker.runner import run_once

log = logging.getLogger("ingest.worker")

# One connection for the message being handled, one for recording its failure.
POOL_MAX_SIZE = 2


def main(settings: Settings) -> None:
    key = settings.blob_account_key.get_secret_value() if settings.blob_account_key else None
    service = service_client(settings.blob_account_url, key)
    ensure_containers(service, Container)
    store = AzureBlobStore(service)
    pipeline = Pipeline(store=store, scanner=EicarScanner(store), clock=SystemClock())
    connection = settings.queue_connection
    with (
        ConnectionPool(settings.database_url, min_size=1, max_size=POOL_MAX_SIZE) as pool,
        ServiceBusQueue.connect(
            settings.queue_name,
            connection_string=connection.get_secret_value() if connection else None,
            namespace=settings.queue_namespace,
            max_wait_seconds=settings.queue_max_wait_seconds,
        ) as queue,
    ):
        log.info("working %s", settings.queue_name)
        while True:  # unbounded by design: a daemon that runs until its container stops
            try:
                run_once(queue, pool.connection, pipeline, batch=settings.worker_batch)
            except QueueError as exc:
                log.warning("receive failed, retrying: %s", exc)
                pipeline.clock.sleep(settings.queue_max_wait_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)  # it logs every HTTP request at INFO
    main(Settings())
