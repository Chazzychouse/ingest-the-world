"""Mints test access tokens with locally generated keys, standing in for an IdP."""

import json
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.test/realms/ingest"
AUDIENCE = "ingest-api"


class TestIdp:
    __test__ = False  # not a pytest test class

    def __init__(self, kid: str = "test-key"):
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [jwk]}

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
        return jwt.encode(payload, self.private_key, algorithm="RS256", headers={"kid": self.kid})
