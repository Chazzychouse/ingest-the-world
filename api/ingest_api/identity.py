"""Who the caller is, as the platform sees them."""

from fastapi import APIRouter

from ingest_api import authz
from ingest_api.deps import Conn, CurrentPrincipal
from ingest_api.schema import ApiModel

router = APIRouter(tags=["identity"])


class Me(ApiModel):
    subject: str
    name: str | None
    username: str | None
    roles: list[str]
    groups: list[str]
    clearance: authz.Marking  # Pydantic serializes the dataclass; no second definition


@router.get("/me")
def me(principal: CurrentPrincipal, conn: Conn) -> Me:
    return Me(
        subject=principal.subject,
        name=principal.name,
        username=principal.username,
        roles=sorted(principal.roles),
        groups=sorted(principal.groups),
        clearance=authz.highest_clearance(conn, principal),
    )
