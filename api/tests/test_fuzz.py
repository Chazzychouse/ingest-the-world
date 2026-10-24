"""Schema-driven fuzzing of every route, as users with different roles and clearances.

Schemathesis generates requests from the OpenAPI schema (valid and malformed), and every
response is checked for server errors (ROB-1) and for CUI reaching an uncleared user (ACC-1).
The app runs on its own database, so the collections and grants fuzzing creates don't touch
other tests.
"""

import psycopg
import pytest
import schemathesis

from tests.conftest import USERS, make_app
from tests.invariants import check

schema = schemathesis.pytest.from_fixture("api_schema")


@pytest.fixture(scope="session")
def fuzz_database(fresh_database):
    return fresh_database()


@pytest.fixture(scope="session")
def api_schema(fuzz_database, idp):
    return schemathesis.openapi.from_asgi("/openapi.json", make_app(fuzz_database, idp))


def _markings(body) -> list[str]:
    """Every `marking` value anywhere in a JSON body."""
    if isinstance(body, dict):
        found = [body["marking"]] if isinstance(body.get("marking"), str) else []
        return found + [m for value in body.values() for m in _markings(value)]
    if isinstance(body, list):
        return [m for value in body for m in _markings(value)]
    return []


def _cui_ids(database_url: str) -> set[str]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute("SELECT id FROM catalog.collections WHERE marking <> 'UNCLASSIFIED'")
        return {r[0] for r in rows}


# Admin with CUI clearance, cleared Contributor, uncleared Reader. Every generated request
# is sent as each of them.
FUZZ_USERS = ["alice", "carol", "bob"]


@pytest.mark.invariant("ROB-1", "ACC-1")
@schema.parametrize()
def test_fuzz(case, auth, fuzz_database):
    for user in FUZZ_USERS:
        _check_response(case, user, case.call(headers=auth(user)), fuzz_database)


def _check_response(case, user, response, fuzz_database):
    check(
        "ROB-1",
        response.status_code < 500,
        f"{case.method} {case.formatted_path} as {user}: {response.status_code} {response.text}",
    )
    if "Clearance.CUI" in USERS[user] or response.status_code == 422:
        return  # 422 bodies echo the caller's own input back
    body = response.json() if response.content else None
    check(
        "ACC-1",
        set(_markings(body)) <= {"UNCLASSIFIED"},
        f"{user} received markings {_markings(body)} from {case.method} {case.formatted_path}",
    )
    leaked = _cui_ids(fuzz_database) & set(response.text.split('"'))
    check("ACC-1", not leaked, f"{user} received CUI ids {leaked} from {case.formatted_path}")
