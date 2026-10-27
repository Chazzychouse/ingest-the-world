"""Preview and scene tokens: how the tiler serves one item's raster, or its 3D Tiles, with no
database and no login.

The API mints one (`POST /items/{id}/preview` or `/scene`) once it has checked the caller may
read the item and audited the request (ACC-5). It names the item, the one blob that serves it (a
COG, or a `.3tz`), and when it expires, at most 15 minutes on, and is signed with a key only the
API and the tiler hold, for one audience: a preview token opens no scene, nor a scene token a
preview. The tiler serves only for a token whose signature holds, that hasn't expired, that is
for its routes, and that names the item the request's path asks for (ACC-9); and it reads only
the blob the token names.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt

from ingest_api.ports import Container

ALGORITHM = "HS256"
AUDIENCE = "ingest-tiler"  # previews
SCENE_AUDIENCE = "ingest-scenes"
# An HMAC key is only as strong as its length: at least the hash's size (RFC 7518, 3.2).
MIN_KEY_LENGTH = 32
CLAIMS = ("aud", "sub", "exp", "container", "blob")
# Where the tiler serves an item's preview: `{TILES_PATH}/{item_id}/tilejson.json`, and its tiles;
# and its scene: `{SCENES_PATH}/{item_id}/tileset.json`, and the files that names.
TILES_PATH = "/tiles/items"
SCENES_PATH = "/scenes"


@dataclass(frozen=True, slots=True)
class Preview:
    item_id: UUID
    container: Container
    blob: str
    expires_at: dt.datetime


def mint(preview: Preview, key: str, *, audience: str = AUDIENCE) -> str:
    claims = {
        "aud": audience,
        "sub": str(preview.item_id),
        "container": preview.container.value,
        "blob": preview.blob,
        # Whole seconds, rounded down: a token never outlives the session it was minted for.
        "exp": int(preview.expires_at.timestamp()),
    }
    return jwt.encode(claims, key, algorithm=ALGORITHM)


def verify(
    token: str, key: str, *, item_id: UUID, now: dt.datetime, audience: str = AUDIENCE
) -> Preview | None:
    """What `token` grants, if its signature holds, it is for `audience`, it names `item_id` and
    it is live at `now`; otherwise None, for every reason alike."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=audience,
            # Expiry is checked against `now`, the clock port's, not PyJWT's own clock.
            options={"require": list(CLAIMS), "verify_exp": False},
        )
    except jwt.InvalidTokenError:
        return None
    expires, container, blob = claims["exp"], claims["container"], claims["blob"]
    if claims["sub"] != str(item_id) or container not in Container or not isinstance(blob, str):
        return None
    if not isinstance(expires, int) or now.timestamp() >= expires:
        return None
    expires_at = dt.datetime.fromtimestamp(expires, dt.UTC)
    return Preview(item_id, Container(container), blob, expires_at)
