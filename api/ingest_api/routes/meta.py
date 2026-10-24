from fastapi import APIRouter, Request

from ingest_api.deps import Conn

router = APIRouter(tags=["meta"])


@router.get("/healthz")
def healthz(conn: Conn) -> dict[str, str]:
    conn.execute("SELECT 1")
    return {"status": "ok"}


@router.get("/config")
def web_config(request: Request) -> dict:
    """Public, non-secret settings for the SPA, so one web build runs against any IdP or cloud."""
    settings = request.app.state.settings
    return {
        "oidc": {
            "authority": settings.web_oidc_authority,
            "client_id": settings.web_oidc_client_id,
            "scope": settings.web_oidc_scope,
        }
    }
