"""`Queue` over Azure Service Bus (and its emulator)."""

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, NoReturn, Self, cast

from azure.identity import DefaultAzureCredential
from azure.servicebus import (
    AutoLockRenewer,
    ServiceBusClient,
    ServiceBusMessage,
    ServiceBusReceivedMessage,
    ServiceBusReceiver,
    ServiceBusSender,
    ServiceBusSubQueue,
)
from azure.servicebus.amqp import AmqpMessageBodyType
from azure.servicebus.exceptions import (
    MessageAlreadySettled,
    MessageLockLostError,
    OperationTimeoutError,
    ServiceBusCommunicationError,
    ServiceBusConnectionError,
    ServiceBusError,
    ServiceBusServerBusyError,
)

from ingest_api.ports import DeadLetter, LockLost, Message, QueueError, TransientQueueError

TRANSIENT_ERRORS = (
    OperationTimeoutError,
    ServiceBusCommunicationError,
    ServiceBusConnectionError,
    ServiceBusServerBusyError,
)
# The Standard tier's message size limit. Anything larger was not sent by Event Grid or by us.
MAX_BODY_BYTES = 256 * 1024
# Dead-letter reason for a message this adapter cannot decode into a `Message`.
UNREADABLE_BODY = "UnreadableBody"


def _raise_translated(exc: ServiceBusError) -> NoReturn:
    """Re-raises an SDK error as the port's error type."""
    if isinstance(exc, MessageLockLostError | MessageAlreadySettled):
        raise LockLost(str(exc)) from exc
    if isinstance(exc, TRANSIENT_ERRORS):
        raise TransientQueueError(str(exc)) from exc
    raise QueueError(str(exc)) from exc


def _decode(message: ServiceBusReceivedMessage) -> dict[str, Any] | None:
    """The message's JSON object body, or None if it doesn't have one."""
    if message.body_type != AmqpMessageBodyType.DATA:
        return None
    raw = b"".join(cast(Iterable[bytes], message.body))
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        body: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return cast(dict[str, Any], body) if isinstance(body, dict) else None


def _id(message: ServiceBusReceivedMessage) -> str:
    # The broker assigns every message a sequence number, unique within the queue.
    return str(message.sequence_number)


class ServiceBusQueue:
    """One queue, through one sender and two peek-lock receivers: the queue's and its
    dead-letter queue's.

    Use as a context manager, which owns the AMQP connection. Settling needs the SDK's message
    object and the receiver it came from, so received messages are held by lock token until they
    are settled; a message whose lock has expired is dropped from that table on the next receive.
    With `max_lock_renewal_seconds`, a lock renewer keeps every held message locked until it is
    settled or that long has passed (ING-10).
    """

    def __init__(
        self,
        client: ServiceBusClient,
        queue_name: str,
        *,
        max_wait_seconds: float,
        max_lock_renewal_seconds: float,
    ):
        self._client = client
        self._queue_name = queue_name
        self._max_wait = max_wait_seconds
        self._sender: ServiceBusSender = client.get_queue_sender(queue_name)
        self._receiver: ServiceBusReceiver = client.get_queue_receiver(queue_name)
        self._dead_receiver: ServiceBusReceiver = client.get_queue_receiver(
            queue_name, sub_queue=ServiceBusSubQueue.DEAD_LETTER
        )
        self._renewal = max_lock_renewal_seconds
        self._renewer = AutoLockRenewer(max_lock_renewal_duration=max_lock_renewal_seconds)
        self._held: dict[str, tuple[ServiceBusReceiver, ServiceBusReceivedMessage]] = {}

    @classmethod
    def connect(
        cls,
        queue_name: str,
        *,
        connection_string: str | None,
        namespace: str | None,
        max_wait_seconds: float,
        max_lock_renewal_seconds: float,
    ) -> Self:
        """With a connection string (the emulator) it authenticates with the shared key in it.
        Otherwise it uses the process's Entra identity against `namespace`; set
        `AZURE_AUTHORITY_HOST` for Azure Government."""
        if connection_string is not None:
            client = ServiceBusClient.from_connection_string(connection_string)
        elif namespace is not None:
            client = ServiceBusClient(namespace, DefaultAzureCredential())
        else:
            raise ValueError("set a queue connection string or namespace")
        return cls(
            client,
            queue_name,
            max_wait_seconds=max_wait_seconds,
            max_lock_renewal_seconds=max_lock_renewal_seconds,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self._renewer.close()
        self._dead_receiver.close()
        self._receiver.close()
        self._sender.close()
        self._client.close()

    def send(self, body: dict[str, Any]) -> None:
        try:
            self._sender.send_messages(
                ServiceBusMessage(json.dumps(body), content_type="application/json")
            )
        except ServiceBusError as exc:
            _raise_translated(exc)

    def receive(self, max_messages: int = 1) -> list[Message]:
        return self._receive(self._receiver, max_messages)

    def receive_dead_letters(self, max_messages: int = 1) -> list[Message]:
        return self._receive(self._dead_receiver, max_messages)

    def _receive(self, receiver: ServiceBusReceiver, max_messages: int) -> list[Message]:
        self._forget_expired()
        try:
            received = receiver.receive_messages(
                max_message_count=max_messages, max_wait_time=self._max_wait
            )
        except ServiceBusError as exc:
            _raise_translated(exc)
        return [m for m in (self._accept(receiver, r) for r in received) if m is not None]

    def _accept(
        self, receiver: ServiceBusReceiver, received: ServiceBusReceivedMessage
    ) -> Message | None:
        """Holds a received message for settlement, or dead-letters it if it can't be read."""
        body = _decode(received)
        if body is None:
            if receiver is self._dead_receiver:  # unreadable dead letters have nowhere to go
                self._settle(lambda: receiver.complete_message(received))
                return None
            self._settle(lambda: receiver.dead_letter_message(received, UNREADABLE_BODY))
            return None
        token = str(received.lock_token)
        self._held[token] = (receiver, received)
        if self._renewal > 0:
            self._renewer.register(receiver, received, max_lock_renewal_duration=self._renewal)
        # The SDK passes on AMQP's delivery-count, the number of *earlier* failed deliveries; the
        # port counts this one too, as Service Bus's other SDKs do.
        attempt = (received.delivery_count or 0) + 1
        return Message(_id(received), body, attempt, token)

    def _forget_expired(self) -> None:
        """Bounds the table: a consumer that never settles a message can't grow it forever."""
        now = datetime.now(UTC)
        expired = [t for t, (_, m) in self._held.items() if (m.locked_until_utc or now) <= now]
        for token in expired:
            del self._held[token]

    def _take(self, message: Message) -> tuple[ServiceBusReceiver, ServiceBusReceivedMessage]:
        held = self._held.pop(message.lock_token, None)
        if held is None:
            raise LockLost(message.id)
        return held

    def _settle(self, settle: Callable[[], None]) -> None:
        try:
            settle()
        except ServiceBusError as exc:
            _raise_translated(exc)

    def complete(self, message: Message) -> None:
        receiver, received = self._take(message)
        self._settle(lambda: receiver.complete_message(received))

    def abandon(self, message: Message) -> None:
        receiver, received = self._take(message)
        self._settle(lambda: receiver.abandon_message(received))

    def dead_letter(self, message: Message, *, reason: str) -> None:
        receiver, received = self._take(message)
        if receiver is self._dead_receiver:
            raise LockLost(f"{message.id} is a dead letter already")
        self._settle(lambda: receiver.dead_letter_message(received, reason))

    def peek_dead_letters(self, max_messages: int) -> list[DeadLetter]:
        dead_letters = self._client.get_queue_receiver(
            self._queue_name, sub_queue=ServiceBusSubQueue.DEAD_LETTER
        )
        try:
            with dead_letters:
                peeked = dead_letters.peek_messages(max_message_count=max_messages)
        except ServiceBusError as exc:
            _raise_translated(exc)
        return [DeadLetter(_id(m), _decode(m) or {}, m.dead_letter_reason or "") for m in peeked]
