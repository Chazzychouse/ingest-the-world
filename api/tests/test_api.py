import uuid

import psycopg
import pytest

from tests.audit_log import AuditLog, Event


def new_id() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def test_health_and_public_config(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/config").json()["oidc"]["client_id"] == "ingest-web"


def test_request_id_is_echoed(client):
    response = client.get("/healthz", headers={"x-request-id": "abc-123"})

    assert response.headers["x-request-id"] == "abc-123"


def test_me_requires_a_token(client):
    response = client.get("/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_me_reports_roles_and_highest_clearance(client, auth):
    body = client.get("/me", headers=auth("carol")).json()

    assert body["username"] == "carol"
    assert "Contributor" in body["roles"]
    assert body["clearance"]["code"] == "CUI"


def test_me_defaults_to_unclassified(client, auth):
    assert client.get("/me", headers=auth("bob")).json()["clearance"]["code"] == "UNCLASSIFIED"


@pytest.mark.invariant("ACC-1")
@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("alice", {"site-survey-alpha", "bio-lab-readings", "range-ops-cui"}),
        ("carol", {"site-survey-alpha", "bio-lab-readings", "range-ops-cui"}),
        ("bob", {"site-survey-alpha", "bio-lab-readings"}),
        ("nobody", set()),
    ],
)
def test_collection_visibility_combines_grants_and_clearance(client, auth, user, expected):
    ids = {c["id"] for c in client.get("/collections", headers=auth(user)).json()}

    assert expected <= ids
    if user != "alice":
        # Only seeded collections are granted to non-admins; tests may add admin-only ones.
        assert ids == expected


@pytest.mark.invariant("ACC-2", "ACC-5")
def test_cui_collection_is_hidden_from_uncleared_reader_and_the_attempt_audited(client, auth, db):
    assert client.get("/collections/range-ops-cui", headers=auth("bob")).status_code == 404
    assert AuditLog(db).last(target_id="range-ops-cui", actor_name="bob").outcome == "denied"


@pytest.mark.invariant("ACC-5")
def test_reading_a_collection_is_audited_with_its_marking(client, auth, db):
    response = client.get("/collections/range-ops-cui", headers=auth("carol"))

    assert response.status_code == 200
    assert response.json()["cui_categories"] == ["CRIT"]
    assert AuditLog(db).last(target_id="range-ops-cui", actor_name="carol") == Event(
        action="collection.read",
        outcome="allowed",
        marking="CUI",
        actor_name="carol",
        target_id="range-ops-cui",
    )


@pytest.mark.invariant("ACC-5")
def test_non_admin_cannot_create_collection_and_is_audited(client, auth, db):
    response = client.post(
        "/collections", json={"id": new_id(), "title": "Nope"}, headers=auth("bob")
    )

    assert response.status_code == 403
    assert AuditLog(db).last(actor_name="bob", outcome="denied").action == "authz.role"


def test_admin_creates_collection(client, auth, db):
    collection_id = new_id()
    body = {
        "id": collection_id,
        "title": "Harbor Scan",
        "marking": "CUI",
        "cui_categories": ["CRIT"],
        "retention": {"retain_days": 365, "legal_hold": True},
    }

    response = client.post("/collections", json=body, headers=auth("alice"))

    assert response.status_code == 201
    assert response.json()["marking_label"] == "CUI"
    assert response.json()["retention"]["legal_hold"] is True
    assert AuditLog(db).last(target_id=collection_id).action == "collection.create"
    duplicate = client.post("/collections", json=body, headers=auth("alice"))
    assert duplicate.status_code == 409


@pytest.mark.invariant("ACC-4")
def test_admin_cannot_create_above_own_clearance(client, auth, db):
    collection_id = new_id()

    response = client.post(
        "/collections",
        json={"id": collection_id, "title": "Too high", "marking": "CUI"},
        headers=auth("admin-no-cui"),
    )

    assert response.status_code == 403
    assert AuditLog(db).last(target_id=collection_id).outcome == "denied"
    assert db.execute(
        "SELECT count(*) FROM catalog.collections WHERE id = %s", (collection_id,)
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "body",
    [
        {"title": "No id"},
        {"id": "Bad_ID", "title": "Bad id"},
        {"id": "x-unclassified", "title": "CUI details", "cui_categories": ["CRIT"]},
        {"id": "x-unknown", "title": "Unknown marking", "marking": "TOP-SECRET"},
    ],
)
def test_rejects_invalid_collections(client, auth, body):
    assert client.post("/collections", json=body, headers=auth("alice")).status_code == 422


@pytest.mark.invariant("ACC-6")
def test_audit_log_is_append_only(db):
    for statement in (
        "UPDATE audit.events SET outcome = 'allowed'",
        "DELETE FROM audit.events",
        "TRUNCATE audit.events",
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(statement)


@pytest.mark.invariant("ACC-5")
def test_listing_collections_is_audited_with_the_highest_marking(client, auth, db):
    client.get("/collections", headers=auth("carol"))

    assert AuditLog(db).last(actor_name="carol", action="collection.list").marking == "CUI"


@pytest.mark.invariant("ACC-4")
def test_admin_cannot_grant_on_a_collection_above_their_clearance(client, auth, db):
    response = client.put(
        "/collections/range-ops-cui/grants/user/sub-bob",
        json={"access": "read"},
        headers=auth("admin-no-cui"),
    )

    assert response.status_code == 404
    assert AuditLog(db).last(target_id="range-ops-cui", actor_name="admin-no-cui") == Event(
        action="grant.put",
        outcome="denied",
        marking="CUI",
        actor_name="admin-no-cui",
        target_id="range-ops-cui",
    )


def test_user_grant_opens_a_collection_to_one_person(client, auth):
    collection_id = new_id()
    client.post("/collections", json={"id": collection_id, "title": "T"}, headers=auth("alice"))
    path = f"/collections/{collection_id}/grants/user/sub-nobody"

    assert client.get(f"/collections/{collection_id}", headers=auth("nobody")).status_code == 404
    assert client.put(path, json={"access": "read"}, headers=auth("alice")).status_code == 200
    assert client.get(f"/collections/{collection_id}", headers=auth("nobody")).status_code == 200
    grants = client.get(f"/collections/{collection_id}/grants", headers=auth("alice")).json()
    assert grants == [{"principal_type": "user", "principal": "sub-nobody", "access": "read"}]
    assert client.delete(path, headers=auth("alice")).status_code == 204
    assert client.get(f"/collections/{collection_id}", headers=auth("nobody")).status_code == 404
