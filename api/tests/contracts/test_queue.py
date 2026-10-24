"""Peek-lock queue contract. Runs against SimQueue; the Service Bus emulator joins in M1."""

import random

import pytest

from tests.fakes import Faults, SimClock, SimQueue


@pytest.fixture
def clock():
    return SimClock()


@pytest.fixture
def queue(clock):
    return SimQueue(clock=clock, rng=random.Random(0), lock_seconds=30, max_delivery_count=3)


def test_received_message_is_hidden_until_its_lock_expires(queue, clock):
    queue.send({"n": 1})
    [first] = queue.receive()

    assert queue.receive() == []
    clock.advance(31)
    [again] = queue.receive()
    assert again.id == first.id and again.delivery_count == 2


def test_completed_message_is_gone(queue, clock):
    queue.send({"n": 1})
    queue.complete(queue.receive()[0])
    clock.advance(31)

    assert queue.receive() == [] and len(queue) == 0


def test_completing_after_the_lock_expired_does_nothing(queue, clock):
    queue.send({"n": 1})
    [message] = queue.receive()
    clock.advance(31)
    queue.complete(message)

    assert len(queue) == 1


def test_abandon_redelivers_immediately(queue):
    queue.send({"n": 1})
    queue.abandon(queue.receive()[0])

    assert queue.receive()[0].delivery_count == 2


def test_dead_letters_after_max_deliveries(queue):
    queue.send({"n": 1})
    for _ in range(3):
        queue.abandon(queue.receive()[0])

    assert queue.receive() == [] and queue.dead_letters == [{"n": 1}]


def test_faults_are_reproducible_from_the_seed(clock):
    def run(seed):
        q = SimQueue(clock=clock, rng=random.Random(seed), faults=Faults(duplicate=0.5))
        for n in range(20):
            q.send({"n": n})
        return [m.body["n"] for m in q.receive(100)]

    assert run(7) == run(7)
    assert len(run(7)) > 20
