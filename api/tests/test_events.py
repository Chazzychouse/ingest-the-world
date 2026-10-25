"""Blob events: the parser reads what Event Grid sends, and what the shim builds parses back."""

import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingest_api.events import (
    MAX_BLOB_NAME,
    BlobCreated,
    InvalidEvent,
    blob_created,
    parse_blob_created,
)
from ingest_api.ports import BlobProps, Container

# Microsoft's documented BlobCreated example, with its container renamed to ours:
# https://learn.microsoft.com/azure/event-grid/event-schema-blob-storage#microsoftstorageblobcreated-event
DOCUMENTED: dict[str, Any] = {
    "topic": "/subscriptions/{subscription-id}/resourceGroups/Storage/providers/"
    "Microsoft.Storage/storageAccounts/my-storage-account",
    "subject": "/blobServices/default/containers/landing/blobs/new-file.txt",
    "eventType": "Microsoft.Storage.BlobCreated",
    "eventTime": "2017-06-26T18:41:00.9584103Z",
    "id": "831e1650-001e-001b-66ab-eeb76e069631",
    "data": {
        "api": "PutBlockList",
        "clientRequestId": "6d79dbfb-0e37-4fc4-981f-442c9ca65760",
        "requestId": "831e1650-001e-001b-66ab-eeb76e000000",
        "eTag": "0x8D4BCC2E4835CD0",
        "contentType": "text/plain",
        "contentLength": 524288,
        "blobType": "BlockBlob",
        "url": "https://my-storage-account.blob.core.windows.net/landing/new-file.txt",
        "sequencer": "00000000000004420000000000028963",
        "storageDiagnostics": {"batchId": "b68529f3-68cd-4744-baa4-3c0498ec19f0"},
    },
    "dataVersion": "",
    "metadataVersion": "1",
}


def _with(path: str, value: object) -> dict[str, Any]:
    event = copy.deepcopy(DOCUMENTED)
    *parents, leaf = path.split(".")
    target = event
    for key in parents:
        target = target[key]
    target[leaf] = value
    return event


def test_parses_the_documented_event():
    assert parse_blob_created(DOCUMENTED) == BlobCreated(
        container=Container.LANDING,
        name="new-file.txt",
        etag="0x8D4BCC2E4835CD0",
        size=524288,
        content_type="text/plain",
    )


def test_a_quoted_etag_compares_equal_to_storage_listings():
    assert parse_blob_created(_with("data.eTag", '"0x8D4"')).etag == "0x8D4"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("eventType", "Microsoft.Storage.BlobDeleted"),
        ("data.api", "PutBlock"),  # staged, not committed
        ("subject", "/blobServices/default/containers/landing/new-file.txt"),
        ("subject", "/blobServices/default/containers/landing/blobs/"),
        ("subject", "/blobServices/default/containers/elsewhere/blobs/x"),
        ("subject", f"/blobServices/default/containers/landing/blobs/{'x' * (MAX_BLOB_NAME + 1)}"),
        ("data.contentLength", -1),
        ("data.eTag", ""),
        ("data", None),
    ],
)
def test_rejects_events_it_cannot_act_on(path, value):
    with pytest.raises(InvalidEvent):
        parse_blob_created(_with(path, value))


# Includes names containing "/blobs/": the subject splits at the first one, after the
# container, and container names can't contain "/".
_segments = st.text(st.characters(codec="utf-8", exclude_categories=["Cs", "Cc"]), max_size=60)
blob_names = st.lists(_segments, min_size=1, max_size=3).map("/blobs/".join).filter(bool)


@given(
    name=blob_names,
    etag=st.from_regex(r"0x[0-9A-F]{15}", fullmatch=True),
    size=st.integers(0, 2**40),
    content_type=st.none() | st.sampled_from(["image/tiff", "model/gltf-binary"]),
)
def test_what_the_shim_builds_parses_back(name, etag, size, content_type):
    blob = BlobProps(Container.LANDING, name, size, etag, content_type)
    event = blob_created(
        blob,
        account_url="http://azurite:10000/devstoreaccount1/",
        event_id="e1",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert parse_blob_created(event) == BlobCreated(
        Container.LANDING, name, etag, size, content_type
    )
