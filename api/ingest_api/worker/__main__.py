"""The pipeline worker: `python -m ingest_api.worker`.

Locally it scans with the EICAR stand-in; Defender for Storage's verdicts replace it with the
cloud deployment.
"""

import argparse
import logging
import sys

from psycopg_pool import ConnectionPool

from ingest_api import wiring
from ingest_api.adapters.eicar_scanner import EicarScanner
from ingest_api.adapters.process_sandbox import Limits, ProcessSandbox
from ingest_api.ports import QueueError, SystemClock
from ingest_api.settings import Settings
from ingest_api.worker import selfcheck
from ingest_api.worker.pipeline import Pipeline
from ingest_api.worker.runner import Retry, run_once, sweep_once

log = logging.getLogger("ingest.worker")

# One connection for the message being handled, one for recording its failure.
POOL_MAX_SIZE = 2


def main(settings: Settings) -> None:
    store = wiring.blob_store(settings)
    limits = Limits(settings.extract_timeout_seconds, settings.extract_memory_mib * 1024 * 1024)
    pipeline = Pipeline(
        store=store,
        scanner=EicarScanner(store),
        clock=SystemClock(),
        sandbox=ProcessSandbox(limits),
    )
    with (
        ConnectionPool(settings.database_url, min_size=1, max_size=POOL_MAX_SIZE) as pool,
        wiring.queue(settings) as queue,
    ):
        log.info("working %s", settings.queue_name)
        retry = Retry(max_deliveries=settings.queue_max_deliveries)
        next_sweep = pipeline.clock.monotonic()
        while True:  # unbounded by design: a daemon that runs until its container stops
            try:
                run_once(queue, pool.connection, pipeline, retry=retry, batch=settings.worker_batch)
                if pipeline.clock.monotonic() >= next_sweep:
                    sweep_once(queue, pool.connection, pipeline, batch=settings.worker_batch)
                    next_sweep = pipeline.clock.monotonic() + settings.dead_letter_sweep_seconds
            except QueueError as exc:
                log.warning("queue unavailable, retrying: %s", exc)
                pipeline.clock.sleep(settings.queue_max_wait_seconds)


if __name__ == "__main__":
    wiring.configure_logging()
    parser = argparse.ArgumentParser(description="The ingest pipeline worker.")
    parser.add_argument(
        "--check", action="store_true", help="check the native readers work, then exit"
    )
    if parser.parse_args().check:
        sys.exit(0 if selfcheck.run() else 1)
    main(Settings())
