"""OIDC access-token verification and the authenticated `Principal`.

Nothing here is Entra-specific. Entra, Azure Government Entra and Keycloak differ only
in issuer, audience and claim names, and all of those are settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx
import jwt

if TYPE_CHECKING:
    from ingest_api.settings import Settings


class Role(StrEnum):
    """Platform roles, lowest to highest. Each role includes the ones before it."""

    READER = "Reader"
    CONTRIBUTOR = "Contributor"
    CURATOR = "Curator"
    ADMIN = "Admin"


ROLE_RANK: dict[str, int] = {role.value: rank for rank, role in enumerate(Role, start=1)}
CLEARANCE_ROLE_PREFIX = "Clearance."
BASELINE_CLEARANCE = "UNCLASSIFIED"


class AuthError(Exception):
    """The token is missing, malformed, or fails verification. Maps to HTTP 401."""


@dataclass(frozen=True)
class Principal:
    subject: str
    name: str | None
    username: str | None
    roles: frozenset[str]
    groups: frozenset[str]
    # Marking codes this principal may read. Ranking them is the database's job
    # (`authz.markings`), so new markings need no code change.
    clearances: frozenset[str]

    def has_role(self, minimum: Role) -> bool:
        needed = ROLE_RANK[minimum]
        return any(ROLE_RANK.get(role, 0) >= needed for role in self.roles)

    @property
    def is_admin(self) -> bool:
        return Role.ADMIN in self.roles

    @property
    def effective_roles(self) -> frozenset[str]:
        """Held roles plus every role they outrank, so a grant to `Reader` covers a Curator.

        Entra app roles don't compose, so the hierarchy is applied here, not in the IdP.
        """
        top = max((ROLE_RANK.get(role, 0) for role in self.roles), default=0)
        return self.roles | {role for role, rank in ROLE_RANK.items() if rank <= top}


class KeySource(Protocol):
    # `Any` because PyJWT accepts several key types (RSA, EC, OKP) and types them loosely.
    def signing_key(self, token: str) -> Any: ...


class StaticJwks:
    """A fixed key set. Used by tests and air-gapped deployments."""

    def __init__(self, jwks: dict[str, Any]):
        self._keys = jwt.PyJWKSet.from_dict(jwks)

    def signing_key(self, token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid")
        for key in self._keys.keys:
            if key.key_id == kid:
                return key.key
        raise AuthError("unknown signing key")


class RemoteJwks:
    """Fetches the IdP's JWKS (discovered from the issuer unless overridden) and caches it."""

    def __init__(self, issuer: str, jwks_uri: str | None = None):
        self._issuer = issuer.rstrip("/")
        self._jwks_uri = jwks_uri
        self._client: jwt.PyJWKClient | None = None

    def _discover(self) -> str:
        url = f"{self._issuer}/.well-known/openid-configuration"
        response = httpx.get(url, timeout=10)
        response.raise_for_status()
        return response.json()["jwks_uri"]

    def signing_key(self, token: str) -> Any:
        if self._client is None:
            self._client = jwt.PyJWKClient(self._jwks_uri or self._discover(), lifespan=3600)
        try:
            return self._client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientError as exc:
            raise AuthError("unable to resolve signing key") from exc


def claim(claims: dict[str, Any], path: str) -> object:
    """The value at a dotted claim path (`realm_access.roles`), or None if any step is missing."""
    value: object = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = cast(dict[str, object], value).get(part)
    return value


def _as_set(value: object) -> frozenset[str]:
    """A claim that may be absent, a single string, or a list, as a set of strings."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset([value])
    if isinstance(value, list | tuple):
        return frozenset(str(v) for v in cast(list[object], value))
    raise AuthError("claim must be a string or a list of strings")


@dataclass(frozen=True, slots=True)
class ClaimNames:
    """Where the token carries roles, groups and clearances. Dotted paths reach into nested
    objects (`realm_access.roles`). The defaults match Entra app roles and our Keycloak realm."""

    roles: str = "roles"
    groups: str = "groups"
    clearance: str = "clearance"


DEFAULT_CLAIMS = ClaimNames()
LEEWAY_SECONDS = 60  # tolerated clock skew between the IdP and this service


class OidcVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        keys: KeySource,
        algorithms: list[str],
        claims: ClaimNames = DEFAULT_CLAIMS,
    ):
        self.issuer = issuer
        self.audience = audience
        self.keys = keys
        self.algorithms = algorithms
        self.claims = claims

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.DecodeError as exc:
            raise AuthError("malformed token") from exc
        # Checked before key lookup so `alg: none` and HMAC-with-public-key tricks never
        # reach the decoder.
        if header.get("alg") not in self.algorithms:
            raise AuthError("disallowed signing algorithm")
        try:
            claims = jwt.decode(
                token,
                key=self.keys.signing_key(token),
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(str(exc)) from exc
        return self._principal(claims)

    def _principal(self, claims: dict[str, Any]) -> Principal:
        roles = _as_set(claim(claims, self.claims.roles))
        # Clearance can arrive as its own claim (e.g. a directory attribute) or as an
        # app role such as `Clearance.CUI`, which is how Entra app roles express it.
        clearances = _as_set(claim(claims, self.claims.clearance)) | {
            role.removeprefix(CLEARANCE_ROLE_PREFIX)
            for role in roles
            if role.startswith(CLEARANCE_ROLE_PREFIX)
        }
        return Principal(
            subject=str(claims["sub"]),
            name=_optional_str(claims.get("name")),
            username=_optional_str(
                claims.get("preferred_username") or claims.get("upn") or claims.get("email")
            ),
            roles=frozenset(r for r in roles if not r.startswith(CLEARANCE_ROLE_PREFIX)),
            groups=_as_set(claim(claims, self.claims.groups)),
            clearances=clearances | {BASELINE_CLEARANCE},
        )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def verifier_from_settings(settings: Settings) -> OidcVerifier:
    return OidcVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        keys=RemoteJwks(settings.oidc_issuer, settings.oidc_jwks_uri),
        algorithms=settings.oidc_algorithms,
        claims=ClaimNames(
            roles=settings.oidc_roles_claim,
            groups=settings.oidc_groups_claim,
            clearance=settings.oidc_clearance_claim,
        ),
    )
