"""Blob events on the ingest queue, in the Event Grid schema.

In the cloud, Event Grid delivers `Microsoft.Storage.BlobCreated` for committed blobs in
`landing/` to the queue (`infra/terraform/messaging.tf`); locally the event shim
(`ingest_api.event_shim`) builds the same events with `blob_created`. Workers read them with
`parse_blob_created`. Schema:
https://learn.microsoft.com/azure/event-grid/event-schema-blob-storage
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ingest_api.ports import BlobProps, Container

BLOB_CREATED = "Microsoft.Storage.BlobCreated"
# The APIs that finish a blob; Put Block alone stages bytes that may never be committed.
CommitApi = Literal["PutBlockList", "PutBlob", "CopyBlob", "FlushWithClose"]
COMMIT_APIS: tuple[str, ...] = get_args(CommitApi)
SUBJECT_PREFIX = "/blobServices/default/containers/"
# Blob Storage's limit on blob names.
MAX_BLOB_NAME = 1024


class InvalidEvent(ValueError):
    """A message that isn't a BlobCreated event this platform can act on."""


@dataclass(frozen=True, slots=True)
class BlobCreated:
    """A committed blob. `(container, name, etag)` identifies one version of it (ING-1)."""

    container: Container
    name: str
    etag: str
    size: int
    content_type: str | None


class _Data(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    api: CommitApi
    etag: str = Field(alias="eTag", min_length=1, max_length=256)
    content_length: int = Field(alias="contentLength", ge=0)
    content_type: str | None = Field(default=None, alias="contentType", max_length=255)
    url: str = Field(max_length=4096)


class _Event(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(max_length=256)
    event_type: Literal["Microsoft.Storage.BlobCreated"] = Field(alias="eventType")
    subject: str = Field(max_length=len(SUBJECT_PREFIX) + 64 + MAX_BLOB_NAME)
    data: _Data


def _location(subject: str) -> tuple[Container, str]:
    """`/blobServices/default/containers/{container}/blobs/{name}` -> (container, name)."""
    container, sep, name = subject.removeprefix(SUBJECT_PREFIX).partition("/blobs/")
    if not subject.startswith(SUBJECT_PREFIX) or not sep or not name:
        raise InvalidEvent(f"not a blob subject: {subject!r}")
    if container not in Container:
        raise InvalidEvent(f"not one of this platform's containers: {container!r}")
    if len(name) > MAX_BLOB_NAME:
        raise InvalidEvent("blob name is too long")
    return Container(container), name


def parse_blob_created(body: dict[str, Any]) -> BlobCreated:
    """The blob a queue message announces. Raises InvalidEvent for anything else."""
    try:
        event = _Event.model_validate(body)
    except ValidationError as exc:
        raise InvalidEvent(str(exc)) from exc
    container, name = _location(event.subject)
    return BlobCreated(
        container=container,
        name=name,
        # Storage quotes ETag headers; the event schema doesn't. Compare them unquoted.
        etag=event.data.etag.strip('"'),
        size=event.data.content_length,
        content_type=event.data.content_type,
    )


def blob_created(
    blob: BlobProps, *, account_url: str, event_id: str, event_time: datetime
) -> dict[str, Any]:
    """The event Event Grid would deliver for `blob` having been committed."""
    return {
        "id": event_id,
        "topic": "/subscriptions/local/resourceGroups/local/providers/Microsoft.Storage"
        "/storageAccounts/local",
        "subject": f"{SUBJECT_PREFIX}{blob.container}/blobs/{blob.name}",
        "eventType": BLOB_CREATED,
        "eventTime": event_time.isoformat(),
        "data": {
            "api": "PutBlockList",  # what browser uploads commit with
            "eTag": blob.etag,
            "contentType": blob.content_type,
            "contentLength": blob.size,
            "blobType": "BlockBlob",
            "url": f"{account_url.rstrip('/')}/{blob.container}/{quote(blob.name)}",
        },
        "dataVersion": "",
        "metadataVersion": "1",
    }
