"""Request and response shapes for upload sessions."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from ingest_api.collections.models import MAX_LIST_ITEMS, NO_NUL_PATTERN, CollectionId
from ingest_api.paging import MAX_TOKEN_LENGTH, position
from ingest_api.schema import ApiModel

# One path segment of a blob name: no separators or control characters, and no trailing dot or
# space, which Blob Storage strips or rejects. That also rules out "." and "..".
FILENAME_PATTERN = r"^[^/\\\x00-\x1f\x7f]*[^/\\\x00-\x1f\x7f. ]$"
MEDIA_TYPE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
# Far above any file we expect (multi-GB meshes) and far below Blob Storage's ~190 TiB limit.
MAX_UPLOAD_BYTES = 2**40

Filename = Annotated[str, Field(min_length=1, max_length=255, pattern=FILENAME_PATTERN)]
MediaType = Annotated[str, Field(max_length=255, pattern=MEDIA_TYPE_PATTERN)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Tag = Annotated[str, Field(min_length=1, max_length=64, pattern=NO_NUL_PATTERN)]
# What the pipeline makes of one landed version (ING-2), and so of the upload it decides.
Outcome = Literal["cataloged", "quarantined", "rejected"]
# `pending` until the pipeline decides; then the outcome of the version it acted on.
UploadStatus = Literal["pending", Outcome, "abandoned"]
# Where an upload is, while it has somewhere to go. Pending: its bytes not yet announced (still
# transferring, or never sent), or a version of them in the pipeline. Cataloged: its derivatives
# being made, or every one that applies settled (ING-13). Quarantined, rejected and abandoned
# uploads are done, and have none.
UploadStage = Literal["awaiting-bytes", "processing", "deriving", "ready"]
DEFAULT_LIST_PAGE = 50
MAX_LIST_PAGE = 100


class Reason(StrEnum):
    """Why the pipeline rejected a version. Quarantine's reason is the scanner's signature."""

    NO_UPLOAD = "no-upload"  # nothing was uploaded under this name
    UPLOAD_CLOSED = "upload-closed"  # the upload already has an outcome
    SUPERSEDED = "superseded"  # a newer version of the blob replaced this one
    SIZE_MISMATCH = "size-mismatch"  # the bytes don't match the size the upload declared
    CHECKSUM_MISMATCH = "checksum-mismatch"  # nor the checksum it declared
    DUPLICATE = "duplicate"  # its collection already has an item with these bytes (ING-9)
    RETRIES_EXHAUSTED = "retries-exhausted"  # still failing on its message's last delivery
    DEAD_LETTERED = "dead-lettered"  # its message was dead-lettered before it had an outcome


class UploadIn(ApiModel):
    collection_id: CollectionId
    filename: Filename
    size_bytes: int = Field(gt=0, le=MAX_UPLOAD_BYTES)
    content_type: MediaType | None = None
    # The client's own checksum, if it computed one; the pipeline verifies it after promotion.
    sha256: Sha256 | None = None
    tags: list[Tag] = Field(default_factory=list[str], max_length=MAX_LIST_ITEMS)


class Upload(UploadIn):
    """An upload as returned by the API; built straight from `store.UploadRow`."""

    model_config = ConfigDict(from_attributes=True)  # merged with ApiModel's config

    id: UUID
    status: UploadStatus
    stage: UploadStage | None  # null once it's quarantined, rejected or abandoned
    attempts: int  # deliveries so far of the version being processed; 0 when none is
    reason: Reason | None  # why it was rejected; null unless it was
    signature: str | None  # what the scanner found, if it was quarantined
    marking: str  # the collection's: everything uploaded into it carries at least this
    marking_rank: int
    created_by: str
    created_at: datetime


class UploadSession(ApiModel):
    """An upload plus the signed URL its bytes go to. The URL is shown once and never stored."""

    upload: Upload
    upload_url: str
    expires_at: datetime


class UploadListIn(ApiModel):
    limit: int = Field(default=DEFAULT_LIST_PAGE, ge=1, le=MAX_LIST_PAGE)
    token: str | None = Field(default=None, max_length=MAX_TOKEN_LENGTH)

    @field_validator("token")
    @classmethod
    def _token(cls, token: str | None) -> str | None:
        if token is not None:
            position(token)
        return token


class UploadPage(ApiModel):
    """The caller's own uploads, newest first."""

    uploads: list[Upload]
    next: str | None  # the token for the following page; null on the last
