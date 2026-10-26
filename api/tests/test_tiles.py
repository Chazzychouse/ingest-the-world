"""Footprint views and their vector tiles, by example. `sim/test_search_sim.py` draws random
views against its model of the filters."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox
from ingest_api.tiles.routes import VIEW_NOT_FOUND
from tests.audit_log import AuditLog
from tests.invariants import check
from tests.items import Cataloged, NewItem, catalog
from tests.tiles import footprints

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
ALPHA = "site-survey-alpha"  # UNCLASSIFIED, readable by every Reader
DENVER = Bbox(-105.0, 39.5, -104.5, 40.0)


def _item(db, **fields) -> Cataloged:
    return catalog(db, replace(NewItem(ALPHA, "f.tif", NOW, bbox=DENVER), **fields))


def _open(client, auth, user: str, **filters) -> dict:
    response = client.post("/tiles/footprints", json=filters, headers=auth(user))
    assert response.status_code == 201, response.text
    return response.json()


ORIGIN = (0, 0, 0)  # zoom 0: the whole world in one tile


def _tile(client, auth, user: str, view: dict, at: tuple[int, int, int] = ORIGIN):
    z, x, y = at
    path = view["tiles"][0].format(z=z, x=x, y=y)
    return client.get(path, headers=auth(user))


def _ids(client, auth, user: str, view: dict, at: tuple[int, int, int] = ORIGIN) -> set[str]:
    response = _tile(client, auth, user, view, at)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    return {feature["id"] for feature in footprints(response.content)}


@pytest.mark.invariant("ACC-1", "ACC-9", "SRCH-1")
def test_a_tile_draws_exactly_the_readable_items_matching_the_view(client, auth, db):
    tag = uuid.uuid4().hex
    shown = _item(db, tags=(tag,), kind=Kind.RASTER)
    other_kind = _item(db, tags=(tag,), kind=Kind.VECTOR)
    secret = _item(db, tags=(tag,), kind=Kind.RASTER, raise_to="CUI")
    _item(db, tags=(tag,), kind=Kind.RASTER, bbox=None)  # nowhere to draw

    view = _open(client, auth, "bob", q=tag, kinds=["raster"])

    assert view["tilejson"] == "3.0.0"
    assert view["vector_layers"][0]["id"] == "footprints"
    drawn = _ids(client, auth, "bob", view)
    check("ACC-9", str(secret.id) not in drawn, "bob's tile drew a CUI item")
    check("SRCH-1", str(other_kind.id) not in drawn, "the tile ignored the kinds filter")
    assert drawn == {str(shown.id)}
    carols = _open(client, auth, "carol", q=tag, kinds=["raster"])
    assert _ids(client, auth, "carol", carols) == {str(shown.id), str(secret.id)}


def test_a_footprint_is_drawn_only_in_the_tiles_it_touches(client, auth, db):
    tag = uuid.uuid4().hex
    made = _item(db, tags=(tag,))
    view = _open(client, auth, "bob", q=tag)

    # Denver is in the north-western quarter of the world at zoom 1.
    assert _ids(client, auth, "bob", view, (1, 0, 0)) == {str(made.id)}
    assert _ids(client, auth, "bob", view, (1, 1, 1)) == set()


def test_polar_footprints_are_clipped_to_the_map_not_refused(client, auth, db):
    tag = uuid.uuid4().hex
    arctic = _item(db, tags=(tag,), bbox=Bbox(-30.0, 60.0, 30.0, 90.0))
    _item(db, tags=(tag,), bbox=Bbox(-30.0, 86.0, 30.0, 90.0))  # wholly beyond Web Mercator

    view = _open(client, auth, "bob", q=tag)

    assert _ids(client, auth, "bob", view) == {str(arctic.id)}


@pytest.mark.invariant("ACC-5")
def test_opening_a_view_is_audited_and_its_tiles_are_not(client, auth, db):
    tag = uuid.uuid4().hex
    _item(db, tags=(tag,))
    _item(db, tags=(tag,), raise_to="CUI")
    audit = AuditLog(db)
    audit.mark()

    bobs = _open(client, auth, "bob", q=tag)
    carols = _open(client, auth, "carol", q=tag)
    [bob_opened, carol_opened] = audit.since()
    audit.mark()
    _ids(client, auth, "bob", bobs)
    _ids(client, auth, "carol", carols, (1, 0, 0))

    check("ACC-5", audit.since() == [], "a tile request was audited")
    for event, marking in ((bob_opened, "UNCLASSIFIED"), (carol_opened, "CUI")):
        check(
            "ACC-5",
            (event.action, event.outcome, event.marking) == ("footprints.view", "allowed", marking),
            f"{event}",
        )


@pytest.mark.invariant("ACC-9")
def test_a_view_serves_only_its_opener_until_it_expires(client, auth, db):
    view = _open(client, auth, "bob")
    view_id = view["tiles"][0].split("/")[3]

    taken = _tile(client, auth, "carol", view)
    assert (taken.status_code, taken.json()) == (404, {"detail": VIEW_NOT_FOUND})
    db.execute(
        "UPDATE catalog.footprint_views SET created_at = created_at - interval '1 hour', "
        "expires_at = created_at - interval '1 minute' WHERE id = %s",
        (view_id,),
    )
    expired = _tile(client, auth, "bob", view)
    assert (expired.status_code, expired.json()) == (404, {"detail": VIEW_NOT_FOUND})
    ghost = client.get(f"/tiles/footprints/{uuid.uuid4()}/0/0/0.mvt", headers=auth("bob"))
    assert ghost.json() == {"detail": VIEW_NOT_FOUND}


@pytest.mark.parametrize(("z", "x", "y"), [(0, 1, 0), (1, 0, 2), (23, 0, 0), (2, -1, 0)])
def test_tiles_off_the_map_are_rejected(client, auth, z, x, y):
    view = _open(client, auth, "bob")

    assert _tile(client, auth, "bob", view, (z, x, y)).status_code == 422
