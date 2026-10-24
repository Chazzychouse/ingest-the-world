"""Liveness and the public configuration the web app boots from."""

from typing import Annotated

from fastapi import APIRouter, Depends

from ingest_api.deps import Conn, Runtime, get_runtime
from ingest_api.schema import ApiModel

router = APIRouter(tags=["meta"])


class Health(ApiModel):
    status: str


class OidcClientConfig(ApiModel):
    authority: str
    client_id: str
    scope: str


class WebConfig(ApiModel):
    oidc: OidcClientConfig


@router.get("/healthz")
def healthz(conn: Conn) -> Health:
    conn.execute("SELECT 1")
    return Health(status="ok")


@router.get("/config")
def web_config(runtime: Annotated[Runtime, Depends(get_runtime)]) -> WebConfig:
    """Public, non-secret settings for the SPA, so one web build runs against any IdP or cloud."""
    settings = runtime.settings
    return WebConfig(
        oidc=OidcClientConfig(
            authority=settings.web_oidc_authority,
            client_id=settings.web_oidc_client_id,
            scope=settings.web_oidc_scope,
        )
    )
