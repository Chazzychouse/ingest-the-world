"""One peek-lock queue contract, run against SimQueue and the Service Bus emulator.

The simulator's faulty queue is only worth trusting if its fault-free behaviour is Service
Bus's. Locks expire in real time on the emulator, so the tests that wait for one cost a few
seconds there; the `contract` queue's short lock (`infra/servicebus/config.json`) keeps that low.
"""

import random
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from azure.servicebus import ServiceBusClient, ServiceBusMessage

from ingest_api.adapters.service_bus import UNREADABLE_BODY
from ingest_api.ports import MAX_DELIVERIES_EXCEEDED, DeadLetter, LockLost, Message, Queue
from tests.conftest import EMULATOR_CONNECTION
from tests.contracts.servicebus import emulator_queue, queue_config
from tests.fakes import Faults, SimClock, SimQueue

QUEUE = "contract"
CONFIG = queue_config(QUEUE)


@dataclass(frozen=True, slots=True)
class Broker:
    queue: Queue
    expire_locks: Callable[[], None]  # waits until every current lock has expired


@pytest.fixture(scope="module")
def emulator() -> Iterator[Queue]:
    """One connection for the module; every test settles what it receives, so the queue is
    empty between tests. Dead letters accumulate, so tests look for their own."""
    with emulator_queue(QUEUE) as queue:
        yield queue


@pytest.fixture(params=["sim", "emulator"])
def broker(request: pytest.FixtureRequest) -> Iterator[Broker]:
    if request.param == "sim":
        clock = SimClock()
        queue = SimQueue(
            clock=clock,
            rng=random.Random(0),
            lock_seconds=CONFIG.lock_seconds,
            max_delivery_count=CONFIG.max_delivery_count,
        )
        yield Broker(queue, lambda: clock.advance(CONFIG.lock_seconds + 1))
        return
    queue = request.getfixturevalue("emulator")
    yield Broker(queue, lambda: time.sleep(CONFIG.lock_seconds + 1))


@pytest.fixture
def body() -> dict[str, str]:
    return {"n": uuid.uuid4().hex}


def _receive_one(queue: Queue) -> Message:
    [message] = queue.receive()
    return message


def _dead_letters_of(queue: Queue, body: dict[str, str]) -> list[DeadLetter]:
    return [d for d in queue.peek_dead_letters(100) if d.body == body]


def test_a_received_message_is_hidden_until_its_lock_expires(broker, body):
    broker.queue.send(body)
    first = _receive_one(broker.queue)

    assert (first.body, first.delivery_count) == (body, 1)
    assert broker.queue.receive() == []
    broker.expire_locks()
    again = _receive_one(broker.queue)
    assert (again.id, again.body, again.delivery_count) == (first.id, body, 2)
    with pytest.raises(LockLost):
        broker.queue.complete(first)  # its lock went with the redelivery
    broker.queue.complete(again)


def test_a_completed_message_is_gone(broker, body):
    broker.queue.send(body)
    broker.queue.complete(_receive_one(broker.queue))
    broker.expire_locks()

    assert broker.queue.receive() == []


def test_settling_twice_raises_lock_lost(broker, body):
    broker.queue.send(body)
    message = _receive_one(broker.queue)
    broker.queue.complete(message)

    with pytest.raises(LockLost):
        broker.queue.abandon(message)


def test_abandon_redelivers_immediately(broker, body):
    broker.queue.send(body)
    broker.queue.abandon(_receive_one(broker.queue))

    again = _receive_one(broker.queue)
    assert (again.body, again.delivery_count) == (body, 2)
    broker.queue.complete(again)


def test_a_message_dead_letters_after_its_last_delivery(broker, body):
    broker.queue.send(body)
    for _ in range(CONFIG.max_delivery_count):
        broker.queue.abandon(_receive_one(broker.queue))

    assert broker.queue.receive() == []
    [dead] = _dead_letters_of(broker.queue, body)
    assert dead.reason == MAX_DELIVERIES_EXCEEDED


def test_dead_lettering_keeps_the_reason_and_stops_delivery(broker, body):
    broker.queue.send(body)
    message = _receive_one(broker.queue)
    broker.queue.dead_letter(message, reason="UnsupportedFormat")

    assert broker.queue.receive() == []
    assert _dead_letters_of(broker.queue, body) == [
        DeadLetter(message.id, body, "UnsupportedFormat")
    ]


def test_a_dead_letter_can_be_received_and_completed(broker, body):
    broker.queue.send(body)
    broker.queue.dead_letter(_receive_one(broker.queue), reason="Swept")

    letters = [m for m in broker.queue.receive_dead_letters(10) if m.body == body]
    assert len(letters) == 1
    broker.queue.complete(letters[0])
    assert _dead_letters_of(broker.queue, body) == []
    assert broker.queue.receive() == []


def test_an_abandoned_dead_letter_stays_dead_lettered(broker, body):
    broker.queue.send(body)
    broker.queue.dead_letter(_receive_one(broker.queue), reason="Swept")
    [letter] = [m for m in broker.queue.receive_dead_letters(10) if m.body == body]

    broker.queue.abandon(letter)
    [again] = [m for m in broker.queue.receive_dead_letters(10) if m.body == body]
    assert again.id == letter.id
    with pytest.raises(LockLost):
        broker.queue.dead_letter(again, reason="Twice")  # a dead letter goes no further
    assert broker.queue.receive() == []


def test_receive_returns_at_most_the_batch_size(broker):
    bodies = [{"n": i} for i in range(3)]
    for b in bodies:
        broker.queue.send(b)

    received: list[Message] = []
    for _ in range(len(bodies)):  # a broker may hand over fewer than asked per call
        batch = broker.queue.receive(2)
        assert len(batch) <= 2
        received += batch
        if len(received) == len(bodies):
            break
    assert sorted(m.body["n"] for m in received) == [0, 1, 2]
    for message in received:
        broker.queue.complete(message)


def test_a_message_that_is_not_a_json_object_is_dead_lettered_not_delivered(emulator):
    bodies = [b"not json", b"[1, 2]"]
    with (
        ServiceBusClient.from_connection_string(EMULATOR_CONNECTION) as client,
        client.get_queue_sender(QUEUE) as sender,
    ):
        sender.send_messages([ServiceBusMessage(b) for b in bodies])

    assert emulator.receive(10) == []
    unreadable = [d for d in emulator.peek_dead_letters(100) if d.reason == UNREADABLE_BODY]
    assert len(unreadable) == len(bodies)


def test_sim_faults_are_reproducible_from_the_seed():
    def run(seed):
        q = SimQueue(clock=SimClock(), rng=random.Random(seed), faults=Faults(duplicate=0.5))
        for n in range(20):
            q.send({"n": n})
        return [m.body["n"] for m in q.receive(100)]

    assert run(7) == run(7)
    assert len(run(7)) > 20
