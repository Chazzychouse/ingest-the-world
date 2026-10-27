"""Scenes, by example: the API mints a token for one readable mesh's 3D Tiles, and the tiler serves
the files of that item's `.3tz` under it, and nothing else, until it expires (ACC-9, ACC-5, and
ACC-3: a derivative is only ever served under its item's marking).

The tiler reads a `MemoryBlobStore` here; its archives are made by the real recipe in each test.
"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ingest_api import previews
from ingest_api.items import store
from ingest_api.items.metadata import TILESET_ROLE, Metadata
from ingest_api.items.routes import NO_SCENE, NOT_FOUND
from ingest_api.ports import Container
from ingest_api.settings import MAX_MAP_SESSION_MINUTES
from ingest_api.tiler.routes import SCENE_NOT_FOUND
from ingest_api.worker.recipes.mesh import Tiles3D
from tests.audit_log import AuditLog
from tests.invariants import check
from tests.items import Cataloged, NewItem, catalog
from tests.meshes import textured_model
from tests.test_previews import ALPHA, NOW, Stack, _cog, _raster

RANGE_OPS = "range-ops-cui"  # CUI: bob (no CUI clearance) can't read it


@pytest.fixture
def stack(database_url) -> Stack:
    return Stack(database_url)


@pytest.fixture(scope="module")
def archive(tmp_path_factory) -> bytes:
    """A textured terrain's tileset, as the derive stage makes it."""
    work = tmp_path_factory.mktemp("scene")
    model = textured_model(work / "terrain.glb", side=40)
    scratch = work / "render"
    scratch.mkdir()
    return Tiles3D().render(model, scratch).read_bytes()


def _mesh(db, stack: Stack, archive: bytes | None, collection: str = ALPHA) -> Cataloged:
    """A mesh item, with a `3dtiles` derivative unless `archive` is None."""
    metadata = Metadata("extracted", 0, properties={"mesh:triangles": 3042})
    made = catalog(db, replace(NewItem(collection, "terrain.glb", NOW), metadata=metadata))
    if archive is None:
        return made
    blob = f"{collection}/{made.id.hex}/{TILESET_ROLE}/v1.3tz"
    stack.blobs.put(Container.DERIVED, blob, archive)
    asset = store.NewAsset(
        made.id, TILESET_ROLE, 1, "application/zip", blob, len(archive), "0" * 64, {},
        "3D Tiles", ("data",),
    )  # fmt: skip
    store.record_asset(db, asset, created_at=NOW)
    return made


def _open(client, auth, item_id: uuid.UUID, user: str = "bob"):
    return client.post(f"/items/{item_id}/scene", headers=auth(user))


@pytest.mark.invariant("ACC-5", "ACC-9")
def test_a_scene_serves_its_items_tileset_and_tiles_until_it_expires(
    client, auth, db, stack, archive
):
    made = _mesh(db, stack, archive)
    audit = AuditLog(db)
    audit.mark()

    response = _open(client, auth, made.id)

    assert response.status_code == 201, response.text
    scene = response.json()
    [event] = audit.since()
    check(
        "ACC-5",
        (event.action, event.outcome, event.target_id) == ("item.scene", "allowed", str(made.id)),
        f"{event}",
    )
    lifetime = datetime.fromisoformat(scene["expires_at"]) - datetime.now(UTC)
    check("ACC-5", lifetime <= timedelta(minutes=MAX_MAP_SESSION_MINUTES), f"lives {lifetime}")
    assert scene["tileset"] == f"/scenes/{made.id}/tileset.json"
    tileset = stack.get(scene["tileset"], scene["token"])
    assert (tileset.status_code, tileset.headers["content-type"]) == (200, "application/json")
    content = json.loads(tileset.content)["root"]["content"]["uri"]
    tile = stack.get(f"/scenes/{made.id}/{content}", scene["token"])
    assert (tile.status_code, tile.headers["content-type"]) == (200, "model/gltf-binary")
    assert tile.content[:4] == b"glTF"
    assert tile.headers["cache-control"].startswith("private, max-age=")

    stack.clock.advance(MAX_MAP_SESSION_MINUTES * 60)
    late = stack.get(scene["tileset"], scene["token"])
    check("ACC-9", late.status_code == 404, f"an expired scene served: {late.status_code}")


@pytest.mark.invariant("ACC-9")
def test_a_scene_token_serves_no_other_item_and_no_preview(
    client, auth, db, stack, archive, tmp_path
):
    mine, other = _mesh(db, stack, archive), _mesh(db, stack, archive)
    raster = _raster(db, stack, _cog(tmp_path))
    token = _open(client, auth, mine.id).json()["token"]
    preview_token = client.post(f"/items/{raster.id}/preview", headers=auth("bob")).json()["token"]

    for path, used in [
        (f"/scenes/{other.id}/tileset.json", token),  # another item's
        (f"/tiles/items/{mine.id}/tilejson.json", token),  # a preview's route
        (f"/scenes/{raster.id}/tileset.json", preview_token),  # a preview's token
        (f"/scenes/{mine.id}/tileset.json", None),
        (f"/scenes/{mine.id}/tileset.json", token[:-2] + "xx"),
    ]:
        response = stack.get(path, used)
        check("ACC-9", response.status_code == 404, f"{path} served: {response.status_code}")


def test_a_file_the_archive_lacks_is_not_found(client, auth, db, stack, archive):
    made = _mesh(db, stack, archive)
    token = _open(client, auth, made.id).json()["token"]

    for path in ("tiles/999999.glb", "tiles/x/../../y", "@3dtilesIndex1@x"):
        response = stack.get(f"/scenes/{made.id}/{path}", token)
        assert response.json() == {"detail": SCENE_NOT_FOUND}, (path, response.text)


@pytest.mark.invariant("ACC-3", "ACC-9")
def test_a_scene_is_opened_only_for_who_may_read_its_item(client, auth, db, stack, archive):
    """A mesh's tiles have no marking of their own: a CUI item's scene is refused to a caller
    without CUI clearance exactly as the item is, and a missing item answers the same."""
    cui = _mesh(db, stack, archive, collection=RANGE_OPS)

    for item_id in (cui.id, uuid.uuid4()):
        response = _open(client, auth, item_id, user="bob")
        check(
            "ACC-3",
            (response.status_code, response.json()) == (404, {"detail": NOT_FOUND}),
            f"bob opened {item_id}: {response.status_code}",
        )
    assert _open(client, auth, cui.id, user="carol").status_code == 201


@pytest.mark.invariant("ACC-5")
def test_an_item_without_3d_tiles_has_no_scene(client, auth, db, stack):
    made = _mesh(db, stack, None)
    audit = AuditLog(db)
    audit.mark()

    response = _open(client, auth, made.id)

    assert (response.status_code, response.json()) == (404, {"detail": NO_SCENE})
    [event] = audit.since()
    check("ACC-5", (event.action, event.outcome) == ("item.scene", "denied"), f"{event}")


def test_a_scene_whose_archive_is_gone_is_not_found(client, auth, db, stack, archive):
    made = _mesh(db, stack, archive)
    token = _open(client, auth, made.id).json()["token"]
    stack.blobs.delete(Container.DERIVED, f"{ALPHA}/{made.id.hex}/{TILESET_ROLE}/v1.3tz")

    response = stack.get(f"/scenes/{made.id}/tileset.json", token)

    assert (response.status_code, response.json()) == (404, {"detail": SCENE_NOT_FOUND})


def test_tokens_are_for_one_audience():
    item_id = uuid.uuid4()
    granted = previews.Preview(item_id, Container.DERIVED, "b", NOW + timedelta(minutes=5))
    scene = previews.mint(granted, "k" * 32, audience=previews.SCENE_AUDIENCE)

    assert previews.verify(scene, "k" * 32, item_id=item_id, now=NOW) is None
    found = previews.verify(
        scene, "k" * 32, item_id=item_id, now=NOW, audience=previews.SCENE_AUDIENCE
    )
    assert found == granted


def test_an_archive_that_is_not_a_3tz_is_unprocessable(client, auth, db, stack, tmp_path: Path):
    made = _mesh(db, stack, b"PK\x05\x06" + bytes(18))
    token = _open(client, auth, made.id).json()["token"]

    response = stack.get(f"/scenes/{made.id}/tileset.json", token)

    assert response.status_code == 422, response.text
