from fastapi import APIRouter
from pydantic import BaseModel

from ingest_api.deps import Conn, CurrentPrincipal

router = APIRouter(tags=["identity"])


class Marking(BaseModel):
    code: str
    label: str
    banner_color: str


class Me(BaseModel):
    subject: str
    name: str | None
    username: str | None
    roles: list[str]
    groups: list[str]
    clearance: Marking


@router.get("/me")
def me(principal: CurrentPrincipal, conn: Conn) -> Me:
    clearance = conn.execute(
        """
        SELECT code, label, banner_color FROM authz.markings
        WHERE rank = authz.clearance_rank(%s)
        """,
        (list(principal.clearances),),
    ).fetchone()
    return Me(
        subject=principal.subject,
        name=principal.name,
        username=principal.username,
        roles=sorted(principal.roles),
        groups=sorted(principal.groups),
        clearance=Marking(**clearance),
    )
