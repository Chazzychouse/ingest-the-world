"""Arbitrary text in every free-text field and path segment never causes a 5xx (ROB-1).

Schema fuzzing covers the routes broadly; this targets the inputs most likely to break the
database layer (NUL bytes, odd Unicode, very long strings), which random schema-driven
generation reaches only rarely.
"""

import uuid
from urllib.parse import quote

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.invariants import check

text = st.text(st.characters(exclude_categories=["Cs"]), max_size=300)


VALID = {"title": "Valid", "description": "", "marking": "CUI", "cui_categories": ["CRIT"]}


# One hostile field per example, the rest valid: otherwise nearly every request is rejected
# by validation of some other field and the hostile value never reaches the database.
@pytest.mark.invariant("ROB-1")
@pytest.mark.parametrize("field", ["title", "description", "cui_categories"])
@given(value=text)
def test_collection_text_fields(client, auth, field, value):
    body = {**VALID, "id": f"rb-{uuid.uuid4().hex[:12]}"}
    body[field] = [value] if field == "cui_categories" else value
    response = client.post("/collections", json=body, headers=auth("alice"))

    check("ROB-1", response.status_code in (201, 422), f"{response.status_code} for {body!r}")


@pytest.mark.invariant("ROB-1")
@given(segment=text.filter(lambda s: s not in ("", ".", "..")))
def test_path_segments(client, auth, segment):
    encoded = quote(segment, safe="")
    for method, path, body in [
        ("GET", f"/collections/{encoded}", None),
        ("PUT", f"/collections/site-survey-alpha/grants/group/{encoded}", {"access": "read"}),
        ("DELETE", f"/collections/{encoded}/grants/user/{encoded}", None),
    ]:
        response = client.request(method, path, json=body, headers=auth("alice"))
        check("ROB-1", response.status_code < 500, f"{method} {path}: {response.status_code}")
        if method == "PUT" and response.status_code == 200:
            client.delete(path, headers=auth("alice"))
