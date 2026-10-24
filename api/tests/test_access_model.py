"""Differential tests: the SQL authorization predicate against the reference model."""

import psycopg
import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingest_api import authz
from ingest_api.auth import ROLE_RANK, Principal
from tests.invariants import check
from tests.model import HIERARCHY, Actor, World
from tests.strategies import ROLES, actors, grant_keys
from tests.tokens import TestIdp


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


def _principal(idp: TestIdp, actor: Actor) -> Principal:
    """The Principal production would build from this actor's token."""
    token = idp.bearer(actor.subject, roles=actor.roles, groups=actor.groups)
    return idp.verifier().verify(token["Authorization"].removeprefix("Bearer "))


@pytest.mark.invariant("ACC-1")
@given(world=worlds(), actor_list=st.lists(actors, min_size=1, max_size=4))
def test_sql_predicate_matches_the_model(tx, idp, world, actor_list):
    _load(tx, world)
    try:
        full = World.from_db(tx)  # the generated world plus the seeded collections
        for actor in actor_list:
            principal = _principal(idp, actor)
            got = {
                row[0] for row in tx.execute(authz.READABLE_IDS, authz.principal_args(principal))
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
def test_effective_roles_match_the_model(idp, roles):
    actor = Actor("s", roles=roles)

    assert _principal(idp, actor).effective_roles == actor.granted_roles()
    assert set(ROLE_RANK) == set(HIERARCHY)
