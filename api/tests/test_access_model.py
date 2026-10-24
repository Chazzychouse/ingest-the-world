"""Differential tests: the SQL authorization predicate against the reference model."""

import psycopg
import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingest_api.auth import ROLE_RANK, Principal
from tests.invariants import check
from tests.model import HIERARCHY, Actor, GrantKey, World

ROLES = [*HIERARCHY, "Clearance.CUI", "Clearance.SECRETISH", "Analyst"]
GROUPS = ["g-alpha", "g-bravo"]
SUBJECTS = ["s-1", "s-2", "s-3"]

actors = st.builds(
    Actor,
    subject=st.sampled_from(SUBJECTS),
    roles=st.frozensets(st.sampled_from(ROLES), max_size=4),
    groups=st.frozensets(st.sampled_from(GROUPS), max_size=2),
)
# Grantee names deliberately overlap across kinds (a group called "Admin", a user called
# "g-alpha"): a grant must match on its own kind only, and disjoint names would hide that.
NAMES = [*ROLES, *GROUPS, *SUBJECTS]
grant_keys = st.builds(GrantKey, st.sampled_from(["role", "group", "user"]), st.sampled_from(NAMES))


@st.composite
def worlds(draw):
    # Markings are data: invent extra ones with arbitrary ranks alongside the built-ins.
    extra = draw(st.dictionaries(st.sampled_from(["SECRETISH", "FOUO"]), st.integers(1, 30)))
    markings = {"UNCLASSIFIED": 0, "CUI": 10}
    for code, rank in extra.items():
        if rank not in markings.values():
            markings[code] = rank
    ids = draw(st.lists(st.from_regex(r"m-[a-z]{3}", fullmatch=True), max_size=6, unique=True))
    world = World(markings=markings)
    for cid in ids:
        world.collections[cid] = draw(st.sampled_from(sorted(markings)))
        world.grants[cid] = draw(
            st.dictionaries(grant_keys, st.sampled_from(["read", "write", "curate"]), max_size=4)
        )
    return world


@pytest.fixture
def tx(database_url):
    """A connection whose writes are rolled back after the test."""
    with psycopg.connect(database_url) as conn:
        yield conn
        conn.rollback()


def _load(conn, world: World) -> None:
    conn.execute("SAVEPOINT example")
    for code, rank in world.markings.items():
        conn.execute(
            "INSERT INTO authz.markings VALUES (%s, %s, %s, '#000') ON CONFLICT DO NOTHING",
            (code, rank, code),
        )
    for cid, marking in world.collections.items():
        conn.execute(
            "INSERT INTO catalog.collections (id, title, marking, created_by) "
            "VALUES (%s, %s, %s, 'model')",
            (cid, cid, marking),
        )
        for key, access in world.grants[cid].items():
            conn.execute(
                "INSERT INTO authz.collection_grants VALUES (%s, %s, %s, %s)",
                (cid, key.principal_type, key.principal, access),
            )


def _principal(actor: Actor) -> Principal:
    clearances = {r.removeprefix("Clearance.") for r in actor.roles if r.startswith("Clearance.")}
    return Principal(
        subject=actor.subject,
        name=None,
        username=None,
        roles=actor.roles,
        groups=actor.groups,
        clearances=frozenset(clearances | {"UNCLASSIFIED"}),
    )


@pytest.mark.invariant("ACC-1")
@given(world=worlds(), actor_list=st.lists(actors, min_size=1, max_size=4))
def test_sql_predicate_matches_the_model(tx, world, actor_list):
    _load(tx, world)
    try:
        full = World.from_db(tx)  # the generated world plus the seeded collections
        for actor in actor_list:
            principal = _principal(actor)
            got = {
                row[0]
                for row in tx.execute(
                    "SELECT authz.readable_collections(%s, %s, %s, %s)",
                    (
                        principal.subject,
                        list(principal.effective_roles),
                        list(principal.groups),
                        list(principal.clearances),
                    ),
                )
            }
            expected = full.readable(actor)
            check(
                "ACC-1",
                got == expected,
                f"{actor}: SQL allows {sorted(got - expected)}, model allows "
                f"{sorted(expected - got)} that SQL refuses",
            )
    finally:
        tx.execute("ROLLBACK TO SAVEPOINT example")


@given(roles=st.frozensets(st.sampled_from(ROLES)))
def test_effective_roles_match_the_model(roles):
    actor = Actor("s", roles=roles)

    assert _principal(actor).effective_roles == actor.granted_roles()
    assert set(ROLE_RANK) == set(HIERARCHY)
