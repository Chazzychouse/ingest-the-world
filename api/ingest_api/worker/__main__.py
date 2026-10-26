"""The workers: `python -m ingest_api.worker` runs the pipeline, and relays the derive requests
it writes to the derive queue; `python -m ingest_api.worker --derive` runs the derive stage.

Locally the pipeline scans with the EICAR stand-in; Defender for Storage's verdicts replace it
with the cloud deployment.
"""

import argparse
import logging
import sys

import psycopg
from psycopg_pool import ConnectionPool

from ingest_api import wiring
from ingest_api.adapters.eicar_scanner import EicarScanner
from ingest_api.adapters.process_sandbox import Limits, ProcessRenderer, ProcessSandbox
from ingest_api.ports import QueueError, SystemClock
from ingest_api.settings import Settings
from ingest_api.worker import outbox, selfcheck
from ingest_api.worker.derive import MAX_OUTPUT_BYTES, Deriver
from ingest_api.worker.pipeline import Pipeline
from ingest_api.worker.runner import (
    Retry,
    derive_once,
    run_once,
    sweep_derive_once,
    sweep_once,
)

log = logging.getLogger("ingest.worker")

# One connection for the message being handled, one for recording its failure.
POOL_MAX_SIZE = 2
# Derive requests relayed after each batch of blob events.
RELAY_BATCH = 100


def ingest(settings: Settings) -> None:
    store = wiring.blob_store(settings)
    limits = Limits(settings.extract_timeout_seconds, settings.extract_memory_mib * 1024 * 1024)
    pipeline = Pipeline(
        store=store,
        scanner=EicarScanner(store),
        clock=SystemClock(),
        sandbox=ProcessSandbox(limits),
    )
    clock, batch = pipeline.clock, settings.worker_batch
    with (
        ConnectionPool(settings.database_url, min_size=1, max_size=POOL_MAX_SIZE) as pool,
        wiring.queue(settings) as queue,
        wiring.queue(settings, name=settings.derive_queue_name) as derive_queue,
    ):
        log.info("working %s, relaying to %s", settings.queue_name, settings.derive_queue_name)
        retry = Retry(max_deliveries=settings.queue_max_deliveries)
        next_sweep = clock.monotonic()
        while True:  # unbounded by design: a daemon that runs until its container stops
            try:
                run_once(queue, pool.connection, pipeline, retry=retry, batch=batch)
                with pool.connection() as conn:
                    outbox.relay(conn, derive_queue, limit=RELAY_BATCH, now=clock.now())
                if clock.monotonic() >= next_sweep:
                    sweep_once(queue, pool.connection, pipeline, batch=batch)
                    next_sweep = clock.monotonic() + settings.dead_letter_sweep_seconds
            except (QueueError, psycopg.OperationalError) as exc:
                # The relay's; the pipeline's own database failures are retried per message.
                log.warning("queue or database unavailable, retrying: %s", exc)
                clock.sleep(settings.queue_max_wait_seconds)


def derive(settings: Settings) -> None:
    settings.derive_scratch_dir.mkdir(parents=True, exist_ok=True)
    limits = Limits(settings.derive_timeout_seconds, settings.derive_memory_mib * 1024 * 1024)
    deriver = Deriver(
        wiring.blob_store(settings),
        SystemClock(),
        settings.derive_scratch_dir,
        ProcessRenderer(limits, max_file_bytes=MAX_OUTPUT_BYTES),
    )
    clock, batch = deriver.clock, settings.worker_batch
    with (
        ConnectionPool(settings.database_url, min_size=1, max_size=POOL_MAX_SIZE) as pool,
        wiring.queue(settings, name=settings.derive_queue_name) as queue,
    ):
        log.info("deriving from %s", settings.derive_queue_name)
        retry = Retry(max_deliveries=settings.queue_max_deliveries)
        next_sweep = clock.monotonic()
        while True:  # unbounded by design: a daemon that runs until its container stops
            try:
                derive_once(queue, pool.connection, deriver, retry=retry, batch=batch)
                if clock.monotonic() >= next_sweep:
                    sweep_derive_once(queue, pool.connection, deriver, batch=batch)
                    next_sweep = clock.monotonic() + settings.dead_letter_sweep_seconds
            except QueueError as exc:
                log.warning("queue unavailable, retrying: %s", exc)
                clock.sleep(settings.queue_max_wait_seconds)


if __name__ == "__main__":
    wiring.configure_logging()
    parser = argparse.ArgumentParser(description="The ingest pipeline and derive workers.")
    parser.add_argument(
        "--check", action="store_true", help="check the native readers work, then exit"
    )
    parser.add_argument("--derive", action="store_true", help="run the derive stage")
    arguments = parser.parse_args()
    if arguments.check:
        sys.exit(0 if selfcheck.run() else 1)
    (derive if arguments.derive else ingest)(Settings())
