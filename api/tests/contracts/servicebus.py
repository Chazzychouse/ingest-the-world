"""The Service Bus emulator connection the contract suites share. Skips cleanly when the
emulator isn't running, unless INGEST_REQUIRE_SERVICEBUS is set (`make` sets it once the
emulator's licence is accepted in .env)."""

import json
import os
import re
import socket
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode, ServiceBusSubQueue

from ingest_api import wiring
from ingest_api.adapters.service_bus import ServiceBusQueue
from tests.conftest import EMULATOR_CONNECTION, app_settings

CONFIG = Path(__file__).resolve().parents[3] / "infra" / "servicebus" / "config.json"
# The emulator's AMQP port; probed first because the SDK retries a dead endpoint for minutes.
AMQP_PORT = 5672
# Batches drained before a test; a queue still not empty after this many is a broken test.
MAX_DRAIN_BATCHES = 20


@dataclass(frozen=True, slots=True)
class QueueConfig:
    lock_seconds: int
    max_delivery_count: int


def duration_seconds(duration: str) -> int:
    """An ISO 8601 duration of the forms the config uses: PT5S, PT5M, PT1H."""
    match = re.fullmatch(r"PT(\d+)([SMH])", duration)
    if match is None:
        raise ValueError(f"unsupported duration {duration!r}")
    return int(match[1]) * {"S": 1, "M": 60, "H": 3600}[match[2]]


def queue_config(name: str) -> QueueConfig:
    """A queue's settings, read from the emulator config compose mounts."""
    [namespace] = json.loads(CONFIG.read_text())["UserConfig"]["Namespaces"]
    [queue] = [q["Properties"] for q in namespace["Queues"] if q["Name"] == name]
    return QueueConfig(duration_seconds(queue["LockDuration"]), queue["MaxDeliveryCount"])


def _require_emulator() -> None:
    host = urlsplit(EMULATOR_CONNECTION.split(";")[0].removeprefix("Endpoint=")).hostname
    try:
        socket.create_connection((host or "localhost", AMQP_PORT), timeout=1).close()
    except OSError as exc:
        if os.environ.get("INGEST_REQUIRE_SERVICEBUS"):
            raise
        pytest.skip(f"Service Bus emulator not reachable ({exc}); see .env.example")


def _purge(name: str) -> None:
    """Deletes everything in the queue and its dead-letter queue, so each test starts empty."""
    with ServiceBusClient.from_connection_string(EMULATOR_CONNECTION) as client:
        for sub_queue in (None, ServiceBusSubQueue.DEAD_LETTER):
            receiver = client.get_queue_receiver(
                name, sub_queue=sub_queue, receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE
            )
            with receiver:
                for _ in range(MAX_DRAIN_BATCHES):
                    if not receiver.receive_messages(max_message_count=100, max_wait_time=0.5):
                        break


@contextmanager
def emulator_queue(name: str) -> Generator[ServiceBusQueue]:
    """The adapter on an emptied emulator queue."""
    _require_emulator()
    _purge(name)
    with wiring.queue(app_settings(database_url=""), name=name) as queue:
        yield queue
