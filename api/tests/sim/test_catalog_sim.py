"""Stateful simulation of the catalog API against the reference model.

Hypothesis generates sequences of actors and operations (list, read, create, grant, revoke),
runs each against the real app and a fresh Postgres database, and after every step checks:

- the response matches what the model says the actor may see or do (ACC-1, ACC-2, ACC-4);
- exactly one audit event was written, with the right outcome and marking (ACC-5);
- the database holds the same collections and grants as the model.

Failures shrink to a minimal sequence of steps and print how to replay them. Runs are
seeded: `make sim SEED=123`. Pipeline actors (uploads, workers, faulty queues) join in M1.
"""

from __future__ import annotations

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

from tests.conftest import make_app, reset_catalog
from tests.invariants import check
from tests.model import Actor, GrantKey, World
from tests.tokens import TestIdp

ROLES = ["Reader", "Contributor", "Curator", "Admin", "Clearance.CUI", "Analyst"]
GROUPS = ["g-alpha", "g-bravo"]
SUBJECTS = [f"s-{n}" for n in range(4)]
NOT_FOUND = {"detail": "collection not found"}
GHOST = "ghost-collection"  # never created: the baseline for ACC-2

collection_ids = st.from_regex(r"c-[a-z]{2}", fullmatch=True)
# Grantee names deliberately overlap across kinds (a group called "Admin", a user called
# "g-alpha"): a grant must match on its own kind only, and disjoint names would hide that.
NAMES = [*ROLES, *GROUPS, *SUBJECTS]
grant_keys = st.builds(GrantKey, st.sampled_from(["role", "group", "user"]), st.sampled_from(NAMES))


class CatalogMachine(RuleBasedStateMachine):
    actors = Bundle("actors")

    def __init__(self, client: TestClient, db: psycopg.Connection, idp: TestIdp):
        super().__init__()
        self.client = client
        self.db = db
        self.idp = idp
        reset_catalog(db)
        self.world = World.from_db(db)
        self.audit_mark = self._audit_max()

    # -- plumbing ---------------------------------------------------------------------

    def _audit_max(self) -> int:
        return self.db.execute("SELECT coalesce(max(id), 0) FROM audit.events").fetchone()[0]

    def _headers(self, actor: Actor) -> dict[str, str]:
        token = self.idp.token(
            sub=actor.subject,
            preferred_username=actor.subject,
            roles=sorted(actor.roles),
            groups=sorted(actor.groups),
        )
        return {"Authorization": f"Bearer {token}"}

    def _call(self, actor: Actor, method: str, path: str, **kwargs):
        self.audit_mark = self._audit_max()
        return self.client.request(method, path, headers=self._headers(actor), **kwargs)

    def _new_audit(self) -> list[tuple]:
        return self.db.execute(
            "SELECT action, outcome, marking, target_id FROM audit.events "
            "WHERE id > %s ORDER BY id",
            (self.audit_mark,),
        ).fetchall()

    def _expect_audit(self, action: str, outcome: str, marking: str | None, target=None):
        events = self._new_audit()
        check("ACC-5", len(events) == 1, f"{action}: expected one audit event, got {events}")
        got_action, got_outcome, got_marking, got_target = events[0]
        check(
            "ACC-5",
            (got_action, got_outcome, got_marking) == (action, outcome, marking)
            and (target is None or got_target == target),
            f"audit {events[0]} != {(action, outcome, marking, target)}",
        )

    def _highest(self, ids) -> str | None:
        markings = [self.world.collections[c] for c in ids]
        return max(markings, key=self.world.markings.__getitem__, default=None)

    # -- actors -----------------------------------------------------------------------

    @initialize(target=actors)
    def admin_with_cui(self):
        return Actor("s-admin", roles=frozenset({"Admin", "Clearance.CUI"}))

    @rule(
        target=actors,
        subject=st.sampled_from(SUBJECTS),
        roles=st.frozensets(st.sampled_from(ROLES), max_size=3),
        groups=st.frozensets(st.sampled_from(GROUPS), max_size=2),
    )
    def actor(self, subject, roles, groups):
        return Actor(subject, roles=roles, groups=groups)

    # -- operations -------------------------------------------------------------------

    @rule(actor=actors)
    def list_collections(self, actor):
        response = self._call(actor, "GET", "/collections")
        got = [c["id"] for c in response.json()]
        expected = self.world.readable(actor)
        check("ACC-1", set(got) == expected, f"{actor} listed {sorted(got)}, model {expected}")
        ranks = [c["marking_rank"] for c in response.json()]
        check("ACC-3", ranks == sorted(ranks, reverse=True), f"not ordered by rank: {ranks}")
        self._expect_audit("collection.list", "allowed", self._highest(got))

    @rule(actor=actors, data=st.data())
    def read_collection(self, actor, data):
        known = sorted(self.world.collections)
        cid = data.draw(st.sampled_from([*known, GHOST]))
        response = self._call(actor, "GET", f"/collections/{cid}")
        if self.world.can_read(actor, cid):
            check("ACC-1", response.status_code == 200, f"{actor} refused readable {cid}")
            self._expect_audit("collection.read", "allowed", self.world.collections[cid], cid)
            return
        self._expect_audit("collection.read", "denied", self.world.collections.get(cid), cid)
        ghost = self.client.get(f"/collections/{GHOST}", headers=self._headers(actor))
        check(
            "ACC-2",
            (response.status_code, response.json())
            == (ghost.status_code, ghost.json())
            == (404, NOT_FOUND),
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
            self._expect_audit("POST /collections", "denied", None)
        elif not self.world.cleared_for(actor, marking):
            check("ACC-4", response.status_code == 403, f"{actor} created {marking} {cid}")
            self._expect_audit("collection.create", "denied", marking, cid)
        elif cid in self.world.collections:
            check("ACC-5", response.status_code == 409, f"duplicate create: {response}")
            self._expect_audit("collection.create", "denied", None, cid)
        else:
            check("ACC-4", response.status_code == 201, f"admin create failed: {response.text}")
            self.world.collections[cid] = marking
            self.world.grants[cid] = {}
            self._expect_audit("collection.create", "allowed", marking, cid)

    def _grant_target(self, data) -> str:
        return data.draw(st.sampled_from([*sorted(self.world.collections), GHOST]))

    def _grant_preconditions(self, actor, cid, response, action) -> bool:
        """Checks refusals; returns True when the operation should have succeeded."""
        if not actor.is_admin:
            check("ACC-4", response.status_code == 403, f"non-admin {action}: {response}")
            events = self._new_audit()
            check("ACC-5", len(events) == 1 and events[0][1] == "denied", f"{events}")
            return False
        if not self.world.can_read(actor, cid):
            # Succeeding hands out access the admin doesn't hold; refusing any other way than
            # a plain 404 tells them the collection exists.
            check("ACC-4", response.status_code >= 400, f"{actor} {action} on {cid} succeeded")
            check("ACC-2", response.json() == NOT_FOUND, f"{actor} {action} on {cid}: {response}")
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
        if self._grant_preconditions(actor, cid, response, "grant.put"):
            check("ACC-4", response.status_code == 200, f"grant failed: {response.text}")
            self.world.grants[cid][key] = access
            self._expect_audit("grant.put", "allowed", self.world.collections[cid], cid)

    @rule(actor=actors, data=st.data())
    def revoke(self, actor, data):
        cid = self._grant_target(data)
        existing = sorted(self.world.grants.get(cid, {}), key=str)
        key = data.draw(st.sampled_from(existing) if existing else grant_keys)
        response = self._call(
            actor, "DELETE", f"/collections/{cid}/grants/{key.principal_type}/{key.principal}"
        )
        if self._grant_preconditions(actor, cid, response, "grant.delete"):
            check("ACC-4", response.status_code == 204, f"revoke failed: {response.text}")
            self.world.grants[cid].pop(key, None)
            self._expect_audit("grant.delete", "allowed", self.world.collections[cid], cid)

    # -- global checks ----------------------------------------------------------------

    @invariant()
    def database_matches_model(self):
        actual = World.from_db(self.db)
        check(
            "ACC-1",
            (actual.collections, actual.grants) == (self.world.collections, self.world.grants),
            "database and model disagree about collections or grants",
        )


@pytest.mark.invariant("ACC-1", "ACC-2", "ACC-3", "ACC-4", "ACC-5")
def test_catalog_simulation(fresh_database, idp):
    # One database and app for the whole run; each example starts from a reset catalog.
    url = fresh_database()
    with TestClient(make_app(url, idp)) as client, psycopg.connect(url, autocommit=True) as db:
        run_state_machine_as_test(lambda: CatalogMachine(client, db, idp))
