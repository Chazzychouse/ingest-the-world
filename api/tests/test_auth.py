import base64
import json

import pytest

from ingest_api.auth import AuthError, ClaimNames, OidcVerifier, Role
from tests.tokens import TestIdp


@pytest.fixture(scope="module")
def idp() -> TestIdp:
    return TestIdp()


def make_verifier(idp: TestIdp, claims: ClaimNames | None = None) -> OidcVerifier:
    return idp.verifier(claims or ClaimNames())


def test_valid_token_yields_principal(idp):
    token = idp.token(sub="u1", name="Ada", preferred_username="ada", roles=["Contributor"])

    principal = make_verifier(idp).verify(token)

    assert principal.subject == "u1"
    assert principal.username == "ada"
    assert principal.roles == {"Contributor"}
    assert principal.has_role(Role.READER)
    assert not principal.has_role(Role.CURATOR)


def test_every_principal_holds_baseline_clearance(idp):
    principal = make_verifier(idp).verify(idp.token())

    assert principal.clearances == {"UNCLASSIFIED"}


def test_clearance_from_app_role_is_split_out_of_roles(idp):
    principal = make_verifier(idp).verify(idp.token(roles=["Reader", "Clearance.CUI"]))

    assert principal.roles == {"Reader"}
    assert principal.clearances == {"UNCLASSIFIED", "CUI"}


def test_clearance_from_dedicated_claim(idp):
    principal = make_verifier(idp).verify(idp.token(clearance="CUI"))

    assert "CUI" in principal.clearances


def test_roles_claim_can_be_a_nested_path(idp):
    token = idp.token(realm_access={"roles": ["Admin"]})

    principal = make_verifier(idp, ClaimNames(roles="realm_access.roles")).verify(token)

    assert principal.is_admin


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.test"},
        {"ttl": -3600},
    ],
    ids=["wrong-audience", "wrong-issuer", "expired"],
)
def test_rejects_invalid_claims(idp, claims):
    with pytest.raises(AuthError):
        make_verifier(idp).verify(idp.token(**claims))


def test_rejects_token_signed_by_unknown_key(idp):
    impostor = TestIdp(kid=idp.kid)

    with pytest.raises(AuthError):
        make_verifier(idp).verify(impostor.token())


def test_rejects_unsigned_token(idp):
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    unsigned = f"{b64({'alg': 'none', 'kid': idp.kid})}.{b64({'sub': 'x'})}."

    with pytest.raises(AuthError):
        make_verifier(idp).verify(unsigned)


def test_rejects_garbage(idp):
    with pytest.raises(AuthError):
        make_verifier(idp).verify("not-a-jwt")
