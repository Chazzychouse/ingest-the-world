"""Item reads and search, by example: the STAC shape, edges the simulator can't name, and
malformed searches. `sim/test_search_sim.py` checks the filters and paging at large."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from ingest_api.items.metadata import Bbox
from ingest_api.items.models import FILE_EXTENSION, MAX_TERMS
from tests.audit_log import AuditLog
from tests.invariants import check
from tests.items import Cataloged, NewItem, catalog

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
ALPHA = "site-survey-alpha"  # UNCLASSIFIED, readable by every Reader


def _item(db, **fields) -> Cataloged:
    return catalog(db, replace(NewItem(ALPHA, "f.tif", NOW), **fields))


def _search(client, auth, user: str, **body):
    response = client.post("/search", json=body, headers=auth(user))
    assert response.status_code == 200, response.text
    return [feature["id"] for feature in response.json()["features"]]


def test_an_item_is_a_stac_item(client, auth, db):
    made = _item(db, tags=("survey",), bbox=Bbox(-105.0, 39.5, -104.5, 40.0))
    db.execute(
        "UPDATE catalog.items SET properties = %s WHERE id = %s",
        (Jsonb({"proj:shape": [512, 512], "datetime": "a header's own idea"}), made.id),
    )

    item = client.get(f"/items/{made.id}", headers=auth("bob")).json()

    sha256 = db.execute("SELECT sha256 FROM catalog.items WHERE id = %s", (made.id,)).fetchone()[0]
    assert (item["type"], item["stac_version"], item["collection"]) == ("Feature", "1.1.0", ALPHA)
    assert FILE_EXTENSION in item["stac_extensions"]
    assert item["bbox"] == [-105.0, 39.5, -104.5, 40.0]
    assert item["geometry"]["type"] == "Polygon"
    properties = item["properties"]
    assert properties["datetime"] == "2026-10-25T12:00:00Z"  # core fields win over a header
    assert properties["proj:shape"] == [512, 512]
    assert (properties["title"], properties["keywords"]) == ("f.tif", ["survey"])
    assert properties["file:checksum"] == "1220" + sha256
    assert {link["rel"] for link in item["links"]} == {"self", "collection"}


def test_an_item_with_no_footprint_has_no_bbox(client, auth, db):
    made = _item(db)

    item = client.get(f"/items/{made.id}", headers=auth("bob")).json()

    assert item["geometry"] is None
    assert "bbox" not in item  # STAC forbids a bbox without a geometry


@pytest.mark.invariant("ACC-1", "ACC-2", "ACC-5")
def test_an_item_marked_above_its_collection_needs_clearance_for_its_marking(client, auth, db):
    made = _item(db, raise_to="CUI")
    ghost = uuid.uuid4()
    audit = AuditLog(db)

    refused = client.get(f"/items/{made.id}", headers=auth("bob"))
    missing = client.get(f"/items/{ghost}", headers=auth("bob"))

    check(
        "ACC-2",
        (refused.status_code, refused.json()) == (missing.status_code, missing.json()),
        f"refused {refused.json()} differs from missing {missing.json()}",
    )
    event = audit.last(target_id=str(made.id))
    check("ACC-5", (event.outcome, event.marking) == ("denied", "CUI"), f"{event}")
    check("ACC-1", _search(client, auth, "bob", ids=[str(made.id)]) == [], "bob found it")
    assert _search(client, auth, "carol", ids=[str(made.id)]) == [str(made.id)]


@pytest.mark.invariant("SRCH-1")
def test_a_bbox_across_the_antimeridian_finds_both_sides(client, auth, db):
    east = _item(db, bbox=Bbox(170, -10, 175, 10))
    west = _item(db, bbox=Bbox(-175, -10, -170, 10))
    middle = _item(db, bbox=Bbox(0, -10, 5, 10))
    ids = [str(item.id) for item in (east, west, middle)]

    found = _search(client, auth, "bob", bbox=[160, -20, -160, 20], ids=ids)

    check("SRCH-1", sorted(found) == sorted(ids[:2]), f"found {found}")


@pytest.mark.invariant("SRCH-1")
def test_search_terms_match_wildcards_literally(client, auth, db):
    percent = _item(db, filename="100% done.tif")
    plain = _item(db, filename="1000 done.tif")
    ids = [str(percent.id), str(plain.id)]

    check("SRCH-1", _search(client, auth, "bob", q="0%", ids=ids) == ids[:1], "% matched all")
    check("SRCH-1", _search(client, auth, "bob", q="0_", ids=ids) == [], "_ matched a char")


def _ring(*corners):
    return {"type": "Polygon", "coordinates": [[*corners, corners[0]]]}


@pytest.mark.parametrize(
    "body",
    [
        {"bbox": [0, 0, 1, 1], "intersects": {"type": "Point", "coordinates": [0, 0]}},
        {"bbox": [0, 10, 1, 1]},  # south of north
        {"bbox": [0, 0, 1]},
        {"bbox": [0, 0, 181, 1]},
        {"intersects": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]}},
        {"intersects": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}},
        {"intersects": _ring(*([[0, 0], [1, 0], [1, 1]] * 3334))},  # over 10,000 positions
        {"datetime": "2026-01-01"},  # no UTC offset
        {"datetime": "2026-02-01T00:00:00Z/2026-01-01T00:00:00Z"},
        {"datetime": "a/b/c"},
        {"datetime": "../.."[:2]},
        {"q": " ".join(["t"] * (MAX_TERMS + 1))},
        {"limit": 0},
        {"limit": 101},
        {"token": "not a token"},
        {"token": "eyJmb28iOiAxfQ=="},  # base64 of JSON that isn't a cursor
        {"kinds": ["hologram"]},
    ],
)
def test_malformed_searches_are_rejected(client, auth, body):
    assert client.post("/search", json=body, headers=auth("bob")).status_code == 422


def test_item_ids_are_uuids(client, auth):
    assert client.get("/items/not-a-uuid", headers=auth("bob")).status_code == 422
