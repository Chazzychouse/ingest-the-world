"""A local stand-in for the IdP: mints tokens and builds the verifier that trusts them."""

import json
import time
import uuid
from collections.abc import Iterable
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from ingest_api.auth import ClaimNames, OidcVerifier, StaticJwks

ISSUER = "https://idp.test/realms/ingest"
AUDIENCE = "ingest-api"
ALGORITHM = "RS256"


class TestIdp:
    __test__ = False  # not a pytest test class

    def __init__(self, kid: str = "test-key"):
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk.update(kid=self.kid, use="sig", alg=ALGORITHM)
        return {"keys": [jwk]}

    def verifier(self, claims: ClaimNames | None = None) -> OidcVerifier:
        return OidcVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            keys=StaticJwks(self.jwks),
            algorithms=[ALGORITHM],
            claims=claims or ClaimNames(),
        )

    def token(self, *, sub: str | None = None, ttl: int = 300, **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": sub or str(uuid.uuid4()),
            "iat": now,
            "exp": now + ttl,
            **claims,
        }
        return jwt.encode(payload, self.private_key, algorithm=ALGORITHM, headers={"kid": self.kid})

    def bearer(
        self, subject: str, *, roles: Iterable[str] = (), groups: Iterable[str] = ()
    ) -> dict[str, str]:
        """Authorization headers for a user whose username is their subject's suffix."""
        token = self.token(
            sub=subject,
            preferred_username=subject.removeprefix("sub-"),
            roles=sorted(roles),
            groups=sorted(groups),
        )
        return {"Authorization": f"Bearer {token}"}
