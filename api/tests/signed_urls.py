"""Checks a signed URL the API issued against ACC-8, from the URL alone.

The limits here restate the invariant (one blob, create and write only, one hour); they are
deliberately not imported from production settings, which are what's under test.
"""

from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

from tests.invariants import check

MAX_LIFETIME = timedelta(hours=1)
UPLOAD_PERMISSIONS = frozenset("cw")


def check_upload_url(url: str, *, container: str, blob_name: str, issued_by: datetime) -> None:
    """`url` writes only `container/blob_name`, and expires within the limit of its issue, which
    happened no later than `issued_by` (take the time after the response arrives)."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    path = unquote(parts.path)
    check("ACC-8", path.endswith(f"/{container}/{blob_name}"), f"URL path {path} != {blob_name}")
    check("ACC-8", query.get("sr") == ["b"], f"URL scope is {query.get('sr')}, not one blob")
    permissions = set("".join(query.get("sp", [])))
    check(
        "ACC-8",
        permissions and permissions <= UPLOAD_PERMISSIONS,
        f"upload URL grants {sorted(permissions)}",
    )
    expiry = datetime.fromisoformat(query["se"][0])
    check(
        "ACC-8",
        expiry <= issued_by + MAX_LIFETIME,
        f"URL expires {expiry - issued_by} after issue",
    )
