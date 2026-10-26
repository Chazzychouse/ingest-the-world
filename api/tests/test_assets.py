"""An item's files, by example: STAC assets, and the route that answers with a read URL for one
of them. `sim/test_catalog_sim.py` downloads at random against the access model."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from ingest_api.items import store
from ingest_api.items.metadata import CLOUD_OPTIMIZED, Metadata
from ingest_api.items.routes import ASSET_NOT_FOUND, NOT_FOUND
from ingest_api.ports import Container
from tests.audit_log import AuditLog
from tests.contracts.azurite import azurite
from tests.invariants import check
from tests.items import Cataloged, NewItem, catalog
from tests.signed_urls import check_read_url

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
ALPHA = "site-survey-alpha"  # UNCLASSIFIED, readable by every Reader
OCTETS = "application/octet-stream"  # what `tests.items` catalogs every original as


def _item(db, **fields) -> Cataloged:
    return catalog(db, replace(NewItem(ALPHA, "ortho.tif", NOW), **fields))


def _derive(db, item: Cataloged, role: str, version: int = 1) -> store.NewAsset:
    """Records a derivative as the derive stage would."""
    asset = store.NewAsset(
        item.id,
        role,
        version,
        "image/png",
        f"{ALPHA}/{item.id.hex}/{role}/v{version}.png",
        100 + version,
        f"{version:064x}",
        {},
        f"{role} v{version}",
        (role,),
    )
    store.record_asset(db, asset, created_at=NOW)
    return asset


def test_an_item_lists_its_original_and_its_newest_derivatives(client, auth, db):
    made = _item(db)
    _derive(db, made, "thumbnail", 1)
    newest = _derive(db, made, "thumbnail", 2)

    item = client.get(f"/items/{made.id}", headers=auth("bob")).json()
    [found] = client.post("/search", json={"ids": [str(made.id)]}, headers=auth("bob")).json()[
        "features"
    ]

    assert found["assets"] == item["assets"]
    assert set(item["assets"]) == {"original", "thumbnail"}
    original = item["assets"]["original"]
    assert original["href"] == f"/items/{made.id}/assets/original"
    assert (original["type"], original["title"], original["roles"]) == (
        OCTETS,
        "ortho.tif",
        ["data"],
    )
    assert original["file:checksum"] == item["properties"]["file:checksum"]
    thumbnail = item["assets"]["thumbnail"]
    assert thumbnail == {
        "href": f"/items/{made.id}/assets/thumbnail",
        "type": "image/png",
        "title": "thumbnail v2",
        "roles": ["thumbnail"],
        "file:size": newest.size_bytes,
        "file:checksum": "1220" + newest.sha256,
    }


def test_a_cloud_optimized_original_says_so(client, auth, db):
    made = _item(db, metadata=Metadata("extracted", 0, properties={CLOUD_OPTIMIZED: True}))

    item = client.get(f"/items/{made.id}", headers=auth("bob")).json()

    assert item["assets"]["original"]["roles"] == ["data", "cloud-optimized"]


@pytest.mark.invariant("ACC-5", "ACC-8", "ACC-9")
@pytest.mark.parametrize("suffix", ["", "/url"])
def test_an_asset_answers_with_a_read_url_for_its_own_blob(client, auth, db, suffix):
    made = _item(db)
    _derive(db, made, "thumbnail", 1)
    newest = _derive(db, made, "thumbnail", 2)
    audit = AuditLog(db)
    expected = {
        "original": (Container.RAW, made.raw_blob, OCTETS),
        "thumbnail": (Container.DERIVED, newest.blob_name, "image/png"),
    }

    for role, (container, blob_name, media_type) in expected.items():
        audit.mark()
        response = client.get(
            f"/items/{made.id}/assets/{role}{suffix}",
            headers=auth("bob"),
            follow_redirects=False,
        )

        url = response.json()["url"] if suffix else response.headers["location"]
        assert response.status_code == (200 if suffix else 307), response.text
        assert response.headers["cache-control"] == "no-store"
        check_read_url(
            url,
            container=container,
            blob_name=blob_name,
            media_type=media_type,
            issued_by=datetime.now(UTC),
        )
        [event] = audit.since()
        check(
            "ACC-5",
            (event.action, event.outcome, event.marking, event.target_id)
            == ("asset.read", "allowed", "UNCLASSIFIED", str(made.id)),
            f"{event}",
        )


@pytest.mark.invariant("ACC-2", "ACC-5", "ACC-9")
@pytest.mark.parametrize("role", ["original", "thumbnail", "nothing-by-this-name"])
def test_an_unreadable_items_files_answer_like_a_missing_items(client, auth, db, role):
    made = _item(db, raise_to="CUI")
    _derive(db, made, "thumbnail")
    audit = AuditLog(db)
    audit.mark()

    for suffix in ("", "/url"):
        refused = client.get(f"/items/{made.id}/assets/{role}{suffix}", headers=auth("bob"))
        missing = client.get(f"/items/{uuid.uuid4()}/assets/{role}{suffix}", headers=auth("bob"))

        check(
            "ACC-9",
            (refused.status_code, refused.json())
            == (missing.status_code, missing.json())
            == (404, {"detail": NOT_FOUND}),
            f"refused {refused.text} differs from missing {missing.text}",
        )
    event = audit.last(target_id=str(made.id))
    check("ACC-5", (event.outcome, event.marking) == ("denied", "CUI"), f"{event}")
    allowed = client.get(
        f"/items/{made.id}/assets/original", headers=auth("carol"), follow_redirects=False
    )
    assert allowed.status_code == 307


@pytest.mark.invariant("ACC-5")
def test_a_readable_item_without_the_asset_answers_404(client, auth, db):
    made = _item(db)
    audit = AuditLog(db)
    audit.mark()

    response = client.get(f"/items/{made.id}/assets/cog", headers=auth("bob"))

    assert (response.status_code, response.json()) == (404, {"detail": ASSET_NOT_FOUND})
    [event] = audit.since()
    check("ACC-5", (event.outcome, event.marking) == ("denied", "UNCLASSIFIED"), f"{event}")


@pytest.mark.invariant("ACC-9")
def test_the_read_url_downloads_the_original_from_storage(client, auth, db):
    content = b"the original's bytes " + uuid.uuid4().bytes
    made = _item(db, content=content, filename="ünï ortho.tif")
    raw = azurite(Container.RAW).get_blob_client(Container.RAW, made.raw_blob)
    raw.upload_blob(content, overwrite=True)

    url = client.get(f"/items/{made.id}/assets/original/url", headers=auth("bob")).json()["url"]
    response = httpx.get(url)

    assert (response.status_code, response.content) == (200, content)
    assert response.headers["content-type"] == OCTETS
    assert "filename*=UTF-8''%C3%BCn%C3%AF%20ortho.tif" in response.headers["content-disposition"]
