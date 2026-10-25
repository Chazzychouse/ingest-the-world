"""Stateful simulation of item reads and search against an independent model of the filters.

Hypothesis catalogs items (some marked above their collection, many sharing a timestamp, some
with no footprint), then reads them and pages through random searches as random actors,
cataloging more items between pages. After every request it checks:

- every result is readable by the actor and matches every filter, as `matches` below decides
  it, written apart from the SQL (ACC-1, SRCH-1);
- an unreadable item answers exactly like a missing one (ACC-2);
- each request wrote one audit event with the highest marking it disclosed (ACC-5);

and at the end of each search, that the pages returned every item that matched when paging
began exactly once, in order, and nothing that never matched (SRCH-2).

Runs are seeded: `make sim SEED=123`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    rule,
    run_state_machine_as_test,
)

from ingest_api.audit import AuditAction
from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Bbox
from ingest_api.items.routes import NOT_FOUND
from tests.audit_log import AuditLog
from tests.conftest import make_app, reset_catalog
from tests.invariants import InvariantViolation, check
from tests.items import Cataloged, NewItem, catalog
from tests.model import Actor, World
from tests.strategies import actors as actor_strategy
from tests.tokens import TestIdp

GHOST_ITEM = "00000000-0000-4000-8000-000000000000"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
# Few distinct times, so many items share one and the cursor's tie-break is exercised.
TIMES = [EPOCH + timedelta(hours=h) for h in range(4)]
# Filenames and terms meant to trip a substring matcher: case, separators, LIKE wildcards.
FILENAMES = ["ortho_2024.tif", "Site-Alpha scan.laz", "100% model.glb", "a_b.csv", "ALPHA.tif"]
TAGS = ["Survey", "lidar", "50%", "x_y"]
TERMS = ["alpha", "ORTHO", "2024", "%", "_", "a_b", "x_y", "scan", "lid", "zzz"]
MAX_PAGES = 200  # far above any search here; a runaway cursor fails rather than loops


@dataclass(frozen=True)
class Query:
    """A search as the model reads it; `body()` is the same search as the API takes it."""

    bbox: tuple[int, int, int, int] | None = None
    point: tuple[int, int] | None = None
    box: tuple[int, int, int, int] | None = None  # sent as a GeoJSON polygon
    start: datetime | None = None
    end: datetime | None = None
    terms: tuple[str, ...] = ()
    collections: tuple[str, ...] | None = None
    kinds: tuple[Kind, ...] | None = None
    ids: tuple[str, ...] | None = None

    def body(self) -> dict:
        body: dict = {}
        if self.bbox:
            body["bbox"] = list(self.bbox)
        if self.point:
            body["intersects"] = {"type": "Point", "coordinates": list(self.point)}
        if self.box:
            w, s, e, n = self.box
            ring = [[w, s], [e, s], [e, n], [w, n], [w, s]]
            body["intersects"] = {"type": "Polygon", "coordinates": [ring]}
        if self.start or self.end:
            ends = [t.isoformat() if t else ".." for t in (self.start, self.end)]
            body["datetime"] = ends[0] if self.start == self.end else "/".join(ends)
        if self.terms:
            body["q"] = " ".join(self.terms)
        for name in ("collections", "kinds", "ids"):
            if (values := getattr(self, name)) is not None:
                body[name] = list(values)
        return body


def _overlaps(a: tuple[float, float, float, float], b: Bbox) -> bool:
    west, south, east, north = a
    return west <= b.east and b.west <= east and south <= b.north and b.south <= north


def _boxes(bbox: tuple[int, int, int, int]) -> list[tuple[float, float, float, float]]:
    west, south, east, north = bbox
    if west <= east:
        return [bbox]
    return [(west, south, 180, north), (-180, south, east, north)]


def _spatial(query: Query, footprint: Bbox | None) -> bool:
    if query.bbox is None and query.point is None and query.box is None:
        return True
    if footprint is None:
        return False
    if query.bbox is not None:
        return any(_overlaps(box, footprint) for box in _boxes(query.bbox))
    if query.point is not None:
        x, y = query.point
        return _overlaps((x, y, x, y), footprint)
    return query.box is not None and _overlaps(query.box, footprint)


def matches(query: Query, cataloged: Cataloged) -> bool:
    """SRCH-1's definition: the item satisfies every filter the query gives."""
    item = cataloged.item
    text = [item.filename.lower(), *(tag.lower() for tag in item.tags)]
    return (
        _spatial(query, item.bbox)
        and (query.start is None or query.start <= item.created_at)
        and (query.end is None or item.created_at <= query.end)
        and all(any(term.lower() in field for field in text) for term in query.terms)
        and (query.collections is None or item.collection in query.collections)
        and (query.kinds is None or item.kind in query.kinds)
        and (query.ids is None or str(cataloged.id) in query.ids)
    )


lons = st.integers(-180, 180).map(lambda v: v - v % 30)
lats = st.integers(-90, 90).map(lambda v: v - v % 30)


@st.composite
def footprints(draw) -> Bbox | None:
    if draw(st.booleans()):
        return None
    west, east = sorted(draw(st.lists(lons, min_size=2, max_size=2, unique=True)))
    south, north = sorted(draw(st.lists(lats, min_size=2, max_size=2, unique=True)))
    return Bbox(west, south, east, north)


@st.composite
def boxes(draw) -> tuple[int, int, int, int]:
    """A query box; west may be east of east, crossing the antimeridian."""
    south, north = sorted(draw(st.lists(lats, min_size=2, max_size=2)))
    return draw(lons), south, draw(lons), north


class SearchMachine(RuleBasedStateMachine):
    actors = Bundle("actors")

    def __init__(self, client: TestClient, db: psycopg.Connection, idp: TestIdp):
        super().__init__()
        self.client, self.db, self.idp = client, db, idp
        self.audit = AuditLog(db)
        reset_catalog(db)
        self.world = World.from_db(db)
        self.items: dict[str, Cataloged] = {}

    def _headers(self, actor: Actor) -> dict[str, str]:
        return self.idp.bearer(actor.subject, roles=actor.roles, groups=actor.groups)

    def _call(self, actor: Actor, method: str, path: str, **kwargs):
        self.audit.mark()
        return self.client.request(method, path, headers=self._headers(actor), **kwargs)

    def _expect_audit(self, action: AuditAction, outcome: str, marking: str | None) -> None:
        events = self.audit.since()
        check("ACC-5", len(events) == 1, f"{action}: expected one audit event, got {events}")
        got = (events[0].action, events[0].outcome, events[0].marking)
        check("ACC-5", got == (action, outcome, marking), f"audit {got}")

    def _readable(self, actor: Actor, item: Cataloged) -> bool:
        return self.world.can_read_item(actor, item.item.collection, item.marking)

    # -- actors and items -------------------------------------------------------------

    @initialize(target=actors)
    def admin_with_cui(self):
        return Actor("s-admin", roles=frozenset({"Admin", "Clearance.CUI"}))

    @rule(target=actors, actor=actor_strategy)
    def actor(self, actor):
        return actor

    def _catalog(self, data) -> None:
        collection = data.draw(st.sampled_from(sorted(self.world.collections)))
        raised = data.draw(st.booleans()) and self.world.collections[collection] != "CUI"
        new = NewItem(
            collection=collection,
            filename=data.draw(st.sampled_from(FILENAMES)),
            created_at=data.draw(st.sampled_from(TIMES)),
            tags=tuple(data.draw(st.lists(st.sampled_from(TAGS), max_size=2, unique=True))),
            kind=data.draw(st.sampled_from(list(Kind))),
            bbox=data.draw(footprints()),
            raise_to="CUI" if raised else None,
        )
        item = catalog(self.db, new)
        self.items[str(item.id)] = item

    @rule(data=st.data())
    def catalog_item(self, data):
        self._catalog(data)

    # -- reads ------------------------------------------------------------------------

    @rule(actor=actors, data=st.data())
    def read_item(self, actor, data):
        # Insertion order, not sorted: ids are random, and draws must not depend on their values.
        item_id = data.draw(st.sampled_from([*self.items, GHOST_ITEM]))
        response = self._call(actor, "GET", f"/items/{item_id}")
        item = self.items.get(item_id)
        if item is not None and self._readable(actor, item):
            check("ACC-1", response.status_code == 200, f"{actor} refused readable {item}")
            check("ACC-1", response.json()["id"] == item_id, f"{item_id} served as another")
            self._expect_audit(AuditAction.ITEM_READ, "allowed", item.marking)
            return
        # The response before the audit: serving it is a leak, whatever the audit says.
        check("ACC-1", response.status_code != 200, f"{actor} was served unreadable {item_id}")
        check(
            "ACC-2",
            (response.status_code, response.json()) == (404, {"detail": NOT_FOUND}),
            f"{actor} on item {item_id}: {response.status_code} {response.text}",
        )
        self._expect_audit(AuditAction.ITEM_READ, "denied", item.marking if item else None)

    # -- search -----------------------------------------------------------------------

    def _query(self, data) -> Query:
        # Half the searches are broad, so they span pages and paging itself is tested (SRCH-2).
        if data.draw(st.booleans()):
            return Query()
        geometry = data.draw(st.sampled_from(["none", "bbox", "point", "box"]))
        box = data.draw(boxes()) if geometry == "box" else None
        if box is not None:  # a GeoJSON polygon is planar: it never wraps the antimeridian
            box = (min(box[0], box[2]), box[1], max(box[0], box[2]), box[3])
        when = data.draw(st.sampled_from(["any", "instant", "between", "since", "until"]))
        a, b = sorted(data.draw(st.lists(st.sampled_from(TIMES), min_size=2, max_size=2)))
        interval = {
            "any": (None, None),
            "instant": (a, a),
            "between": (a, b),
            "since": (a, None),
            "until": (None, b),
        }
        start, end = interval[when]
        # Insertion order, not sorted: ids are random, and draws must not depend on their values.
        ids = st.lists(st.sampled_from([*self.items, GHOST_ITEM]), max_size=4).map(tuple)
        return Query(
            bbox=data.draw(boxes()) if geometry == "bbox" else None,
            point=(data.draw(lons), data.draw(lats)) if geometry == "point" else None,
            box=box,
            start=start,
            end=end,
            terms=tuple(data.draw(st.lists(st.sampled_from(TERMS), max_size=2))),
            collections=data.draw(
                st.none()
                | st.lists(st.sampled_from(sorted(self.world.collections)), max_size=4).map(tuple)
            ),
            kinds=data.draw(
                st.none() | st.lists(st.sampled_from(list(Kind)), max_size=4).map(tuple)
            ),
            ids=data.draw(st.none() | ids),
        )

    def _expected(self, actor: Actor, query: Query) -> set[str]:
        return {
            i
            for i, item in self.items.items()
            if self._readable(actor, item) and matches(query, item)
        }

    def _check_page(self, actor: Actor, query: Query, features: list[dict]) -> None:
        for feature in features:
            item = self.items.get(feature["id"])
            check(
                "ACC-1",
                item is not None and self._readable(actor, item),
                f"{actor} was shown {feature['id']}",
            )
            if item is not None:
                check("SRCH-1", matches(query, item), f"{item} doesn't match {query}")
        markings = [self.items[f["id"]].marking for f in features if f["id"] in self.items]
        highest = max(markings, key=self.world.markings.__getitem__, default=None)
        self._expect_audit(AuditAction.ITEM_SEARCH, "allowed", highest)

    @rule(actor=actors, limit=st.integers(1, 6), data=st.data())
    def search(self, actor, limit, data):
        query = self._query(data)
        at_start = self._expected(actor, query)
        body = {**query.body(), "limit": limit}
        seen: list[str] = []
        positions: list[tuple[datetime, uuid.UUID]] = []
        for _ in range(MAX_PAGES):
            response = self._call(actor, "POST", "/search", json=body)
            check("SRCH-1", response.status_code == 200, f"{body}: {response.text}")
            page = response.json()
            self._check_page(actor, query, page["features"])
            check("SRCH-2", len(page["features"]) <= limit, f"page longer than {limit}")
            seen += [f["id"] for f in page["features"]]
            positions += [
                (self.items[i].item.created_at, uuid.UUID(i)) for i in seen[len(positions) :]
            ]
            if not page["links"]:
                break
            check("SRCH-2", len(page["features"]) == limit, "a short page that isn't the last")
            body = {**body, **page["links"][0]["body"]}
            if data.draw(st.booleans()):
                self._catalog(data)
        else:
            raise InvariantViolation("SRCH-2", f"no last page after {MAX_PAGES} pages")
        at_end = self._expected(actor, query)
        check("SRCH-2", len(seen) == len(set(seen)), f"returned twice: {seen}")
        check("SRCH-2", at_start <= set(seen), f"never returned: {at_start - set(seen)}")
        check("SRCH-2", set(seen) <= at_end, f"returned non-matches: {set(seen) - at_end}")
        check("SRCH-2", positions == sorted(positions, reverse=True), "pages out of order")


@pytest.mark.invariant("ACC-1", "ACC-2", "ACC-5", "SRCH-1", "SRCH-2")
def test_search_simulation(fresh_database, idp):
    url = fresh_database()
    with TestClient(make_app(url, idp)) as client, psycopg.connect(url, autocommit=True) as db:
        # Hypothesis accepts any factory here; its annotation only says `type`.
        run_state_machine_as_test(lambda: SearchMachine(client, db, idp))  # pyright: ignore[reportArgumentType]
