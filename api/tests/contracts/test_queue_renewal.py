"""Lock renewal (ING-10), against SimQueue's lock model and the Service Bus emulator's lock
renewer. Kept apart from `test_queue.py`, whose module-wide emulator connection could otherwise
take the message this module sends."""

import random
import time
import uuid
from collections.abc import Iterator

import pytest

from tests.contracts.servicebus import emulator_queue
from tests.contracts.test_queue import CONFIG, QUEUE, Broker, _receive_one
from tests.fakes import SimClock, SimQueue
from tests.invariants import check

# Long enough that a lock would lapse at least once without renewal.
RENEWAL_SECONDS = 4 * CONFIG.lock_seconds


@pytest.fixture(params=["sim", "emulator"])
def renewing(request: pytest.FixtureRequest) -> Iterator[Broker]:
    """A broker whose adapter renews locks while messages are held (ING-10)."""
    if request.param == "sim":
        clock = SimClock()
        queue = SimQueue(
            clock=clock,
            rng=random.Random(0),
            lock_seconds=CONFIG.lock_seconds,
            max_delivery_count=CONFIG.max_delivery_count,
            max_lock_renewal_seconds=RENEWAL_SECONDS,
        )
        yield Broker(queue, lambda: clock.advance(2 * CONFIG.lock_seconds + 1))
        return
    with emulator_queue(QUEUE, lock_renewal_seconds=RENEWAL_SECONDS) as queue:
        yield Broker(queue, lambda: time.sleep(2 * CONFIG.lock_seconds + 1))


@pytest.mark.invariant("ING-10")
def test_a_held_message_keeps_its_lock_past_the_lock_duration(renewing):
    body = {"n": uuid.uuid4().hex}
    renewing.queue.send(body)
    message = _receive_one(renewing.queue)

    renewing.expire_locks()  # twice the lock duration: unrenewed, it would be redelivered
    check("ING-10", renewing.queue.receive() == [], "redelivered while still held")
    renewing.queue.complete(message)  # the lock is still this worker's
