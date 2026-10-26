"""Stateful simulation of the catalog and upload API against the reference model.

Hypothesis generates sequences of actors and operations (list, read, create, grant, revoke,
start an upload, read it, renew its URL, list one's own, download an item's file, preview a
raster), runs each
against the real app and a fresh Postgres database, and after every step checks:

- the response matches what the model says the actor may see or do (ACC-1, ACC-2, ACC-4,
  ACC-7, ACC-9), every signed URL is scoped to its upload's or asset's blob (ACC-8), and every
  preview token names only its item and the COG that previews it (ACC-9);
- exactly one audit event was written, with the right outcome and marking (ACC-5);
- the database holds the same collections, grants and uploads as the model.

Failures shrink to a minimal sequence of steps and print how to replay them. Runs are
seeded: `make sim SEED=123`. The pipeline has its own machine, `test_pipeline_sim.py`.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from ingest_api import previews
from ingest_api.audit import AuditAction
from ingest_api.collections.routes import NOT_FOUND
from ingest_api.items import store as items
from ingest_api.items.metadata import CLOUD_OPTIMIZED, COG_ROLE, Metadata
from ingest_api.items.routes import ASSET_NOT_FOUND, NO_PREVIEW
from ingest_api.items.routes import NOT_FOUND as ITEM_NOT_FOUND
from ingest_api.ports import Container
from ingest_api.settings import MAX_MAP_SESSION_MINUTES
from ingest_api.uploads.routes import NOT_FOUND as UPLOAD_NOT_FOUND
from tests.audit_log import AuditLog
from tests.conftest import PREVIEW_KEY, make_app, reset_catalog
from tests.invariants import InvariantViolation, check
from tests.items import NewItem, catalog
from tests.model import Actor, World
from tests.signed_urls import check_read_url, check_upload_url
from tests.strategies import actors as actor_strategy
from tests.strategies import grant_keys
from tests.tokens import TestIdp

GHOST = "ghost-collection"  # never created: the baseline for ACC-2
GHOST_UPLOAD = "00000000-0000-4000-8000-000000000000"
collection_ids = st.from_regex(r"c-[a-z]{2}", fullmatch=True)
MAX_PAGES = 50  # far above the uploads one example makes; a runaway token fails, not loops
filenames = st.sampled_from(["scan.laz", "site model.glb", "100%.tif", "ünï.csv"])
GHOST_ITEM = "00000000-0000-4000-8000-000000000001"
THUMBNAIL = "thumbnail"
OCTETS = "application/octet-stream"  # what `tests.items` catalogs every original as


@dataclass(frozen=True, slots=True)
class Item:
    collection: str
    marking: str
    blobs: dict[str, tuple[Container, str, str]]  # asset key -> (container, blob, media type)
    previewed_from: tuple[Container, str] | None  # its COG, if it has one


class CatalogMachine(RuleBasedStateMachine):
    actors = Bundle("actors")

    def __init__(self, client: TestClient, db: psycopg.Connection, idp: TestIdp):
        super().__init__()
        self.client = client
        self.idp = idp
        self.db = db
        self.audit = AuditLog(db)
        reset_catalog(db)
        self.world = World.from_db(db)
        self.uploads: dict[str, tuple[str, str]] = {}  # id -> (collection, creator's subject)
        self.items: dict[str, Item] = {}  # cataloged by the pipeline, not through the API

    # -- plumbing ---------------------------------------------------------------------

    def _headers(self, actor: Actor) -> dict[str, str]:
        return self.idp.bearer(actor.subject, roles=actor.roles, groups=actor.groups)

    def _call(self, actor: Actor, method: str, path: str, **kwargs):
        self.audit.mark()
        return self.client.request(method, path, headers=self._headers(actor), **kwargs)

    def _expect_audit(
        self, action: AuditAction, outcome: str, marking: str | None, target: str | None = None
    ) -> None:
        events = self.audit.since()
        check("ACC-5", len(events) == 1, f"{action}: expected one audit event, got {events}")
        event = events[0]
        check(
            "ACC-5",
            (event.action, event.outcome, event.marking) == (action, outcome, marking)
            and (target is None or event.target_id == target),
            f"audit {event} != {(action, outcome, marking, target)}",
        )

    def _highest(self, ids) -> str | None:
        markings = [self.world.collections[c] for c in ids]
        return max(markings, key=self.world.markings.__getitem__, default=None)

    # -- actors -----------------------------------------------------------------------

    @initialize(target=actors)
    def admin_with_cui(self):
        return Actor("s-admin", roles=frozenset({"Admin", "Clearance.CUI"}))

    @rule(target=actors, actor=actor_strategy)
    def actor(self, actor):
        return actor

    # -- operations -------------------------------------------------------------------

    @rule(actor=actors)
    def list_collections(self, actor):
        response = self._call(actor, "GET", "/collections")
        got = [c["id"] for c in response.json()]
        expected = self.world.readable(actor)
        check("ACC-1", set(got) == expected, f"{actor} listed {sorted(got)}, model {expected}")
        ranks = [c["marking_rank"] for c in response.json()]
        check("ACC-3", ranks == sorted(ranks, reverse=True), f"not ordered by rank: {ranks}")
        uploadable = {c["id"] for c in response.json() if c["can_upload"]}
        writable = self.world.writable(actor)
        check("ACC-7", uploadable == writable, f"{actor} can upload {uploadable}, model {writable}")
        self._expect_audit(AuditAction.COLLECTION_LIST, "allowed", self._highest(got))

    @rule(actor=actors, data=st.data())
    def read_collection(self, actor, data):
        known = sorted(self.world.collections)
        cid = data.draw(st.sampled_from([*known, GHOST]))
        response = self._call(actor, "GET", f"/collections/{cid}")
        if self.world.can_read(actor, cid):
            check("ACC-1", response.status_code == 200, f"{actor} refused readable {cid}")
            self._expect_audit(
                AuditAction.COLLECTION_READ, "allowed", self.world.collections[cid], cid
            )
            return
        self._expect_audit(
            AuditAction.COLLECTION_READ, "denied", self.world.collections.get(cid), cid
        )
        ghost = self.client.get(f"/collections/{GHOST}", headers=self._headers(actor))
        check(
            "ACC-2",
            (response.status_code, response.json())
            == (ghost.status_code, ghost.json())
            == (404, {"detail": NOT_FOUND}),
            f"{actor} on {cid}: {response.status_code} {response.json()} differs from a "
            f"missing collection's {ghost.status_code} {ghost.json()}",
        )

    @rule(
        actor=actors,
        cid=collection_ids,
        marking=st.sampled_from(["UNCLASSIFIED", "CUI"]),
    )
    def create_collection(self, actor, cid, marking):
        response = self._call(
            actor, "POST", "/collections", json={"id": cid, "title": cid, "marking": marking}
        )
        if not actor.is_admin:
            check("ACC-4", response.status_code == 403, f"non-admin create: {response}")
            self._expect_audit(AuditAction.ROLE_CHECK, "denied", None)
        elif not self.world.cleared_for(actor, marking):
            check("ACC-4", response.status_code == 403, f"{actor} created {marking} {cid}")
            self._expect_audit(AuditAction.COLLECTION_CREATE, "denied", marking, cid)
        elif cid in self.world.collections:
            check("ACC-5", response.status_code == 409, f"duplicate create: {response}")
            self._expect_audit(AuditAction.COLLECTION_CREATE, "denied", None, cid)
        else:
            check("ACC-4", response.status_code == 201, f"admin create failed: {response.text}")
            self.world.collections[cid] = marking
            self.world.grants[cid] = {}
            self._expect_audit(AuditAction.COLLECTION_CREATE, "allowed", marking, cid)

    def _grant_target(self, data) -> str:
        return data.draw(st.sampled_from([*sorted(self.world.collections), GHOST]))

    def _grant_preconditions(self, actor, cid, response, action) -> bool:
        """Checks refusals; returns True when the operation should have succeeded."""
        if not actor.is_admin:
            check("ACC-4", response.status_code == 403, f"non-admin {action}: {response}")
            self._expect_audit(AuditAction.ROLE_CHECK, "denied", None)
            return False
        if not self.world.can_read(actor, cid):
            # Succeeding hands out access the admin doesn't hold; refusing any other way than
            # a plain 404 tells them the collection exists.
            check("ACC-4", response.status_code >= 400, f"{actor} {action} on {cid} succeeded")
            check(
                "ACC-2",
                response.json() == {"detail": NOT_FOUND},
                f"{actor} {action} on {cid}: {response}",
            )
            self._expect_audit(action, "denied", self.world.collections.get(cid), cid)
            return False
        return True

    @rule(actor=actors, key=grant_keys, access=st.sampled_from(["read", "write"]), data=st.data())
    def grant(self, actor, key, access, data):
        cid = self._grant_target(data)
        response = self._call(
            actor,
            "PUT",
            f"/collections/{cid}/grants/{key.principal_type}/{key.principal}",
            json={"access": access},
        )
        if self._grant_preconditions(actor, cid, response, AuditAction.GRANT_PUT):
            check("ACC-4", response.status_code == 200, f"grant failed: {response.text}")
            self.world.grants[cid][key] = access
            self._expect_audit(AuditAction.GRANT_PUT, "allowed", self.world.collections[cid], cid)

    @rule(actor=actors, data=st.data())
    def revoke(self, actor, data):
        cid = self._grant_target(data)
        existing = sorted(self.world.grants.get(cid, {}), key=str)
        key = data.draw(st.sampled_from(existing) if existing else grant_keys)
        response = self._call(
            actor, "DELETE", f"/collections/{cid}/grants/{key.principal_type}/{key.principal}"
        )
        if self._grant_preconditions(actor, cid, response, AuditAction.GRANT_DELETE):
            check("ACC-4", response.status_code == 204, f"revoke failed: {response.text}")
            self.world.grants[cid].pop(key, None)
            self._expect_audit(
                AuditAction.GRANT_DELETE, "allowed", self.world.collections[cid], cid
            )

    # -- uploads ----------------------------------------------------------------------

    def _check_session(self, session) -> None:
        upload_id = session["upload"]["id"]
        row = self.db.execute(
            "SELECT blob_name FROM ingest.uploads WHERE id = %s", (upload_id,)
        ).fetchone()
        if row is None:
            raise InvariantViolation("ACC-8", f"signed URL for unknown upload {upload_id}")
        check_upload_url(
            session["upload_url"],
            container=Container.LANDING,
            blob_name=row[0],
            issued_by=datetime.now(UTC),
        )

    @rule(actor=actors, filename=filenames, data=st.data())
    def start_upload(self, actor, filename, data):
        cid = data.draw(st.sampled_from([*sorted(self.world.collections), GHOST]))
        response = self._call(
            actor,
            "POST",
            "/uploads",
            json={"collection_id": cid, "filename": filename, "size_bytes": 10},
        )
        marking = self.world.collections.get(cid)
        if not self.world.can_read(actor, cid):
            check(
                "ACC-2",
                (response.status_code, response.json()) == (404, {"detail": NOT_FOUND}),
                f"{actor} upload into unreadable {cid}: {response.status_code} {response.text}",
            )
            self._expect_audit(AuditAction.UPLOAD_CREATE, "denied", marking, cid)
            return
        if not self.world.can_write(actor, cid):
            check("ACC-7", response.status_code == 403, f"{actor} uploaded into {cid}")
            self._expect_audit(AuditAction.UPLOAD_CREATE, "denied", marking, cid)
            return
        check("ACC-7", response.status_code == 201, f"{actor} refused upload: {response.text}")
        session = response.json()
        self._check_session(session)
        upload_id = session["upload"]["id"]
        self.uploads[upload_id] = (cid, actor.subject)
        self._expect_audit(AuditAction.UPLOAD_CREATE, "allowed", marking, upload_id)

    def _upload_target(self, data) -> tuple[str, str | None]:
        """An upload id to act on, and its collection (None for the ghost)."""
        # Insertion order, not sorted: ids are random, and draws must not depend on their values.
        upload_id = data.draw(st.sampled_from([*self.uploads, GHOST_UPLOAD]))
        return upload_id, self.uploads.get(upload_id, (None, ""))[0]

    def _visible_in(self, actor, response, action, upload_id, cid: str | None) -> str | None:
        """The upload's collection if the actor can read it; otherwise checks the refusal."""
        if cid is not None and self.world.can_read(actor, cid):
            return cid
        check(
            "ACC-2",
            (response.status_code, response.json()) == (404, {"detail": UPLOAD_NOT_FOUND}),
            f"{actor} on upload {upload_id}: {response.status_code} {response.text}",
        )
        marking = self.world.collections.get(cid) if cid else None
        self._expect_audit(action, "denied", marking, upload_id)
        return None

    @rule(actor=actors, data=st.data())
    def read_upload(self, actor, data):
        upload_id, cid = self._upload_target(data)
        response = self._call(actor, "GET", f"/uploads/{upload_id}")
        action = AuditAction.UPLOAD_READ
        cid = self._visible_in(actor, response, action, upload_id, cid)
        if cid is None:
            return
        check("ACC-1", response.status_code == 200, f"{actor} refused readable upload")
        self._expect_audit(action, "allowed", self.world.collections[cid], upload_id)

    @rule(actor=actors, data=st.data())
    def renew_upload_url(self, actor, data):
        upload_id, cid = self._upload_target(data)
        response = self._call(actor, "POST", f"/uploads/{upload_id}/url")
        action = AuditAction.UPLOAD_RENEW
        cid = self._visible_in(actor, response, action, upload_id, cid)
        if cid is None:
            return
        marking = self.world.collections[cid]
        creator = self.uploads[upload_id][1]
        if creator != actor.subject or not self.world.can_write(actor, cid):
            check("ACC-7", response.status_code == 403, f"{actor} renewed {creator}'s upload")
            self._expect_audit(action, "denied", marking, upload_id)
            return
        check("ACC-7", response.status_code == 200, f"{actor} refused renewal: {response.text}")
        self._check_session(response.json())
        self._expect_audit(action, "allowed", marking, upload_id)

    @rule(actor=actors, limit=st.integers(min_value=1, max_value=3))
    def list_uploads(self, actor, limit):
        """Pages through the actor's own uploads: exactly those in collections they can read."""
        expected = {
            upload_id
            for upload_id, (cid, creator) in self.uploads.items()
            if creator == actor.subject and self.world.can_read(actor, cid)
        }
        shown: list[str] = []
        token = None
        for _ in range(MAX_PAGES):
            params = {"limit": limit} | ({"token": token} if token else {})
            response = self._call(actor, "GET", "/uploads", params=params)
            check("ACC-1", response.status_code == 200, f"{actor} listing: {response.text}")
            page = [u["id"] for u in response.json()["uploads"]]
            shown += page
            self._expect_audit(AuditAction.UPLOAD_LIST, "allowed", self._highest_upload(page))
            token = response.json()["next"]
            if token is None:
                break
        else:
            raise InvariantViolation("ACC-1", f"{actor}'s upload list never ended")
        check("ACC-1", len(shown) == len(set(shown)), f"{actor} was shown an upload twice")
        check("ACC-1", set(shown) == expected, f"{actor} listed {shown}, model {expected}")

    def _highest_upload(self, upload_ids) -> str | None:
        return self._highest(self.uploads[u][0] for u in upload_ids)

    # -- items and their files --------------------------------------------------------

    @rule(
        data=st.data(),
        item_id=st.uuids(version=4),
        thumbnail=st.booleans(),
        cog=st.sampled_from([None, "derived", "original"]),
    )
    def catalog_item(self, data, item_id, thumbnail, cog):
        """The pipeline catalogs a file, perhaps marked above its collection and perhaps a COG
        already, and the derive stage perhaps thumbnails it or makes it a COG."""
        if str(item_id) in self.items:  # Hypothesis likes to repeat itself
            return
        cid = data.draw(st.sampled_from(sorted(self.world.collections)))
        floor = self.world.markings[self.world.collections[cid]]
        above = sorted(m for m, rank in self.world.markings.items() if rank > floor)
        raise_to = data.draw(st.sampled_from([None, *above]))
        properties = {CLOUD_OPTIMIZED: cog == "original"}
        new = NewItem(cid, "ortho.tif", datetime.now(UTC), id=item_id, raise_to=raise_to)
        metadata = Metadata("extracted", 0, properties=properties, crs="EPSG:32613")
        made = catalog(self.db, replace(new, metadata=metadata))
        blobs = {"original": (Container.RAW, made.raw_blob, OCTETS)}
        for role, wanted in ((THUMBNAIL, thumbnail), (COG_ROLE, cog == "derived")):
            if wanted:
                blobs[role] = self._derive(item_id, cid, role)
        previewed_from = {"original": "original", "derived": COG_ROLE}.get(cog or "")
        self.items[str(item_id)] = Item(
            cid,
            made.marking,
            blobs,
            blobs[previewed_from][:2] if previewed_from else None,
        )

    def _derive(self, item_id: UUID, cid: str, role: str) -> tuple[Container, str, str]:
        blob = f"{cid}/{item_id.hex}/{role}/v1.png"
        asset = items.NewAsset(
            item_id, role, 1, "image/png", blob, 1, "0" * 64, {}, role.title(), (role,)
        )
        items.record_asset(self.db, asset, created_at=datetime.now(UTC))
        return Container.DERIVED, blob, "image/png"

    @rule(
        actor=actors,
        data=st.data(),
        role=st.sampled_from(["original", THUMBNAIL]),
        suffix=st.sampled_from(["", "/url"]),
    )
    def download(self, actor, data, role, suffix):
        """Asks for one of an item's files: a read URL for exactly its blob if the model says the
        actor may read the item, and a missing item's 404 otherwise (ACC-9)."""
        item_id = data.draw(st.sampled_from([*self.items, GHOST_ITEM]))
        path = f"/items/{item_id}/assets/{role}{suffix}"
        response = self._call(actor, "GET", path, follow_redirects=False)
        item = self.items.get(item_id)
        action = AuditAction.ASSET_READ
        if item is None or not self.world.can_read_item(actor, item.collection, item.marking):
            check(
                "ACC-9",
                (response.status_code, response.json()) == (404, {"detail": ITEM_NOT_FOUND}),
                f"{actor} on {path}: {response.status_code} {response.text}",
            )
            self._expect_audit(action, "denied", item and item.marking, item_id)
            return
        if role not in item.blobs:
            check("ACC-9", response.json() == {"detail": ASSET_NOT_FOUND}, response.text)
            self._expect_audit(action, "denied", item.marking, item_id)
            return
        check("ACC-9", response.status_code in (200, 307), f"{actor} refused {path}")
        url = response.json()["url"] if suffix else response.headers["location"]
        container, blob, media_type = item.blobs[role]
        check_read_url(
            url,
            container=container,
            blob_name=blob,
            media_type=media_type,
            issued_by=datetime.now(UTC),
        )
        self._expect_audit(action, "allowed", item.marking, item_id)

    @rule(actor=actors, data=st.data())
    def preview(self, actor, data):
        """Opens a preview of an item: a token naming exactly that item and its COG, for at most
        15 minutes, if the model says the actor may read it, and a missing item's 404 otherwise."""
        item_id = data.draw(st.sampled_from([*self.items, GHOST_ITEM]))
        response = self._call(actor, "POST", f"/items/{item_id}/preview")
        item = self.items.get(item_id)
        action = AuditAction.ITEM_PREVIEW
        if item is None or not self.world.can_read_item(actor, item.collection, item.marking):
            check(
                "ACC-9",
                (response.status_code, response.json()) == (404, {"detail": ITEM_NOT_FOUND}),
                f"{actor} previewed {item_id}: {response.status_code} {response.text}",
            )
            self._expect_audit(action, "denied", item and item.marking, item_id)
            return
        if item.previewed_from is None:
            check("ACC-9", response.json() == {"detail": NO_PREVIEW}, response.text)
            self._expect_audit(action, "denied", item.marking, item_id)
            return
        check("ACC-9", response.status_code == 201, f"{actor} refused a preview of {item_id}")
        self._check_token(response.json()["token"], UUID(item_id), item.previewed_from)
        self._expect_audit(action, "allowed", item.marking, item_id)

    def _check_token(self, token: str, item_id: UUID, source: tuple[Container, str]) -> None:
        now = datetime.now(UTC)
        granted = previews.verify(token, PREVIEW_KEY, item_id=item_id, now=now)
        check(
            "ACC-9",
            granted is not None and (granted.container, granted.blob) == source,
            f"token for {item_id} grants {granted}, not {source}",
        )
        lifetime = granted.expires_at - now if granted else timedelta()
        check("ACC-5", lifetime <= timedelta(minutes=MAX_MAP_SESSION_MINUTES), f"lives {lifetime}")
        for other in (*self.items, str(uuid4())):
            if other != str(item_id):
                elsewhere = previews.verify(token, PREVIEW_KEY, item_id=UUID(other), now=now)
                check("ACC-9", elsewhere is None, f"token for {item_id} serves {other}")

    # -- global checks ----------------------------------------------------------------

    @invariant()
    def database_matches_model(self):
        actual = World.from_db(self.db)
        check(
            "ACC-1",
            (actual.collections, actual.grants) == (self.world.collections, self.world.grants),
            "database and model disagree about collections or grants",
        )
        uploads = {
            str(upload_id): (cid, creator)
            for upload_id, cid, creator in self.db.execute(
                "SELECT id, collection_id, created_by FROM ingest.uploads"
            )
        }
        for item_id in self.items:  # each cataloged from an upload of its own
            uploads.pop(item_id, None)
        check("ACC-7", uploads == self.uploads, "database and model disagree about uploads")


@pytest.mark.invariant("ACC-1", "ACC-2", "ACC-3", "ACC-4", "ACC-5", "ACC-7", "ACC-8", "ACC-9")
def test_catalog_simulation(fresh_database, idp):
    # One database and app for the whole run; each example starts from a reset catalog.
    url = fresh_database()
    with TestClient(make_app(url, idp)) as client, psycopg.connect(url, autocommit=True) as db:
        # Hypothesis accepts any factory here; its annotation only says `type`.
        run_state_machine_as_test(lambda: CatalogMachine(client, db, idp))  # pyright: ignore[reportArgumentType]
