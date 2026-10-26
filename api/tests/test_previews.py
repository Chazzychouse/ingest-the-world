"""Previews, by example: the API mints a token for one readable raster, and the tiler serves that
item's TileJSON and tiles under it, and nothing else, until it expires (ACC-9, ACC-5).

The tiler reads a `MemoryBlobStore` here; its COGs are written by GDAL in each test.
"""

import io
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import morecantile
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient

from ingest_api import previews
from ingest_api.app import create_app as create_api
from ingest_api.items import store
from ingest_api.items.metadata import CLOUD_OPTIMIZED, COG_ROLE, Metadata
from ingest_api.items.routes import NO_PREVIEW, NOT_FOUND, PREVIEWS_OFF
from ingest_api.ports import Container
from ingest_api.settings import MAX_MAP_SESSION_MINUTES
from ingest_api.tiler.app import NoPreviewKey
from ingest_api.tiler.app import create_app as create_tiler
from ingest_api.tiler.routes import PREVIEW_NOT_FOUND
from ingest_api.worker.recipes.raster import Cog
from tests.audit_log import AuditLog
from tests.conftest import PREVIEW_KEY, app_settings
from tests.fakes import MemoryBlobStore, SimClock
from tests.invariants import check
from tests.items import Cataloged, NewItem, catalog
from tests.test_raster_recipes import UTM_13N, render, rgb, write_raster

ALPHA = "site-survey-alpha"  # UNCLASSIFIED, readable by every Reader
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
WEB_MERCATOR = morecantile.tms.get("WebMercatorQuad")
# The tiles read back here are plain PNGs, with no place of their own.
pytestmark = pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")


class Stack:
    """The API's test client, and a tiler over a store of its own, on a clock the test moves."""

    def __init__(self, database_url: str):
        self.blobs = MemoryBlobStore()
        self.clock = SimClock(datetime.now(UTC))
        settings = app_settings(database_url)
        self.tiler = TestClient(create_tiler(settings, blobs=self.blobs, clock=self.clock))

    def get(self, path: str, token: str | None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.tiler.get(path, headers=headers)


@pytest.fixture
def stack(database_url) -> Stack:
    return Stack(database_url)


def _cog(tmp_path: Path, pixels: np.ndarray | None = None) -> bytes:
    """`pixels` (an RGB photo by default) in a COG, as the derive stage makes one."""
    path = tmp_path / f"{uuid.uuid4().hex}.tif"
    if pixels is None:
        write_raster(path, rgb(512, 512), photometric="RGB")
    else:
        write_raster(path, pixels)
    return render(Cog(), path, tmp_path).read_bytes()


def _raster(db, stack: Stack, cog: bytes, **fields) -> Cataloged:
    """A raster item, previewed from a `cog` derivative, or from its original if it says it is
    cloud-optimized."""
    metadata = Metadata("extracted", 0, properties=fields.pop("properties", {}), crs=UTM_13N)
    made = catalog(db, replace(NewItem(ALPHA, "ortho.tif", NOW), metadata=metadata, **fields))
    if metadata.properties.get(CLOUD_OPTIMIZED) is True:
        stack.blobs.put(Container.RAW, made.raw_blob, cog)
        return made
    blob = _cog_blob(made)
    stack.blobs.put(Container.DERIVED, blob, cog)
    asset = store.NewAsset(
        made.id, COG_ROLE, 1, "image/tiff", blob, len(cog), "0" * 64, {}, "COG", ("data",)
    )
    store.record_asset(db, asset, created_at=NOW)
    return made


def _cog_blob(made: Cataloged) -> str:
    return f"{ALPHA}/{made.id.hex}/{COG_ROLE}/v1.tif"


def _open(client, auth, item_id: uuid.UUID, user: str = "bob") -> dict:
    response = client.post(f"/items/{item_id}/preview", headers=auth(user))
    assert response.status_code == 201, response.text
    return response.json()


def _centre_tile(tilejson: dict) -> str:
    """The tile at the raster's centre, at its finest zoom, from the TileJSON's template."""
    west, south, east, north = tilejson["bounds"]
    z = tilejson["maxzoom"]
    at = WEB_MERCATOR.tile((west + east) / 2, (south + north) / 2, z)
    return tilejson["tiles"][0].format(z=at.z, x=at.x, y=at.y)


def _png(content: bytes) -> np.ndarray:
    with rasterio.MemoryFile(io.BytesIO(content)) as memory, memory.open() as image:
        return image.read()


@pytest.mark.invariant("ACC-5", "ACC-9")
def test_a_preview_serves_its_items_tiles_until_it_expires(client, auth, db, stack, tmp_path):
    made = _raster(db, stack, _cog(tmp_path))
    audit = AuditLog(db)
    audit.mark()

    preview = _open(client, auth, made.id)

    [event] = audit.since()
    check(
        "ACC-5",
        (event.action, event.outcome, event.marking, event.target_id)
        == ("item.preview", "allowed", "UNCLASSIFIED", str(made.id)),
        f"{event}",
    )
    expires_at = datetime.fromisoformat(preview["expires_at"])
    lifetime = expires_at - datetime.now(UTC)
    check("ACC-5", lifetime <= timedelta(minutes=MAX_MAP_SESSION_MINUTES), f"lives {lifetime}")
    assert preview["tilejson"] == f"/tiles/items/{made.id}/tilejson.json"
    tilejson = stack.get(preview["tilejson"], preview["token"])
    assert tilejson.status_code == 200, tilejson.text
    west, south, east, north = tilejson.json()["bounds"]
    assert -105.1 < west < east < -104.9 and 39.6 < south < north < 39.8  # UTM 13N, near Denver
    tile = stack.get(_centre_tile(tilejson.json()), preview["token"])
    assert (tile.status_code, tile.headers["content-type"]) == (200, "image/png"), tile.text
    assert _png(tile.content).shape == (4, 256, 256)  # red, green, blue and alpha
    assert tile.headers["cache-control"].startswith("private, max-age=")

    stack.clock.advance((expires_at - stack.clock.now()).total_seconds())
    expired = stack.get(_centre_tile(tilejson.json()), preview["token"])
    check("ACC-9", expired.status_code == 404, f"an expired token served {expired.status_code}")


@pytest.mark.invariant("ACC-9")
def test_a_token_serves_only_the_item_it_was_minted_for(client, auth, db, stack, tmp_path):
    mine, other = (_raster(db, stack, _cog(tmp_path)) for _ in range(2))
    token = _open(client, auth, mine.id)["token"]
    theirs = stack.get(
        f"/tiles/items/{other.id}/tilejson.json", _open(client, auth, other.id)["token"]
    )
    template = theirs.json()["tiles"][0]
    forged = previews.mint(
        # Everything right, the item's own COG included, but the key.
        previews.Preview(
            other.id, Container.DERIVED, _cog_blob(other), datetime.now(UTC) + timedelta(days=1)
        ),
        "a-key-that-is-not-the-tilers-0123456789",
    )

    for path in (theirs.request.url.path, _centre_tile(theirs.json())):
        for wrong in (token, forged, None):
            refused = stack.get(path, wrong)
            check(
                "ACC-9",
                (refused.status_code, refused.json()) == (404, {"detail": PREVIEW_NOT_FOUND}),
                f"{path} with {wrong!r}: {refused.status_code} {refused.text}",
            )
    assert template.startswith(f"http://testserver/tiles/items/{other.id}/")


@pytest.mark.invariant("ACC-2", "ACC-5", "ACC-9")
def test_an_unreadable_item_has_no_preview_like_a_missing_one(client, auth, db, stack, tmp_path):
    made = _raster(db, stack, _cog(tmp_path), raise_to="CUI")
    audit = AuditLog(db)
    audit.mark()

    refused = client.post(f"/items/{made.id}/preview", headers=auth("bob"))
    missing = client.post(f"/items/{uuid.uuid4()}/preview", headers=auth("bob"))

    check(
        "ACC-9",
        (refused.status_code, refused.json())
        == (missing.status_code, missing.json())
        == (404, {"detail": NOT_FOUND}),
        f"refused {refused.text} differs from missing {missing.text}",
    )
    event = audit.last(target_id=str(made.id))
    check("ACC-5", (event.outcome, event.marking) == ("denied", "CUI"), f"{event}")
    assert _open(client, auth, made.id, user="carol")["token"]


@pytest.mark.invariant("ACC-5")
def test_an_item_without_a_cog_has_no_preview(client, auth, db):
    made = catalog(db, NewItem(ALPHA, "notes.txt", NOW))
    audit = AuditLog(db)
    audit.mark()

    response = client.post(f"/items/{made.id}/preview", headers=auth("bob"))

    assert (response.status_code, response.json()) == (404, {"detail": NO_PREVIEW})
    [event] = audit.since()
    check("ACC-5", (event.outcome, event.marking) == ("denied", "UNCLASSIFIED"), f"{event}")


@pytest.mark.invariant("ACC-9")
def test_a_cloud_optimized_original_is_previewed_from_itself(client, auth, db, stack, tmp_path):
    made = _raster(db, stack, _cog(tmp_path), properties={CLOUD_OPTIMIZED: True})

    preview = _open(client, auth, made.id)

    granted = previews.verify(preview["token"], PREVIEW_KEY, item_id=made.id, now=datetime.now(UTC))
    assert granted is not None
    assert (granted.container, granted.blob) == (Container.RAW, made.raw_blob)
    assert stack.get(preview["tilejson"], preview["token"]).status_code == 200


def test_samples_beyond_8_bits_share_one_stretch_across_tiles(client, auth, db, stack, tmp_path):
    ramp = np.arange(512 * 512, dtype=np.uint16).reshape(1, 512, 512)
    made = _raster(db, stack, _cog(tmp_path, ramp))
    preview = _open(client, auth, made.id)

    tilejson = stack.get(preview["tilejson"], preview["token"]).json()

    template = tilejson["tiles"][0]
    assert "?low=" in template and "&high=" in template
    tile = stack.get(_centre_tile(tilejson), preview["token"])
    assert tile.status_code == 200
    assert _png(tile.content).shape == (2, 256, 256)  # grey and alpha


def test_a_tile_the_raster_doesnt_reach_is_empty(client, auth, db, stack, tmp_path):
    made = _raster(db, stack, _cog(tmp_path))
    preview = _open(client, auth, made.id)

    far = stack.get(f"/tiles/items/{made.id}/10/0/0.png", preview["token"])

    assert far.status_code == 204


def test_previews_need_a_key(auth, db, database_url, idp):
    made = catalog(db, NewItem(ALPHA, "ortho.tif", NOW))
    settings = app_settings(database_url).model_copy(update={"preview_key": None})
    with pytest.raises(NoPreviewKey):
        create_tiler(settings, blobs=MemoryBlobStore())
    with TestClient(create_api(settings, idp.verifier())) as keyless:
        response = keyless.post(f"/items/{made.id}/preview", headers=auth("bob"))
    assert (response.status_code, response.json()) == (503, {"detail": PREVIEWS_OFF})
