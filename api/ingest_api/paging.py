"""Keyset pagination, newest first with ties broken by id, for every list the API pages.

A page token names the last row its holder was shown. It is not a secret: every page is
filtered afresh (ACC-1), so a token can skip rows but never reveal one.
"""

import base64
import binascii
import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from ingest_api.schema import ApiModel

MAX_TOKEN_LENGTH = 128


@dataclass(frozen=True, slots=True)
class Position:
    """Where a page ended: the newest-first order resumes strictly after this row."""

    created_at: dt.datetime
    id: UUID


class _Cursor(ApiModel):
    created_at: dt.datetime
    id: UUID


def token(position: Position) -> str:
    cursor = _Cursor(created_at=position.created_at, id=position.id)
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode()


def position(page_token: str) -> Position:
    """The position `page_token` names; raises ValueError if it names none."""
    try:
        cursor = _Cursor.model_validate_json(base64.urlsafe_b64decode(page_token.encode()))
    except (binascii.Error, ValueError):
        raise ValueError("not a page token") from None
    return Position(cursor.created_at, cursor.id)
