"""Checks a signed URL the API issued against ACC-8, from the URL alone.

The limits here restate the invariant (one blob; create and write for an hour to upload, read
for 15 minutes to download); they are deliberately not imported from production settings, which
are what's under test.
"""

from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

from tests.invariants import check

UPLOAD_LIFETIME = timedelta(hours=1)
UPLOAD_PERMISSIONS = frozenset("cw")
READ_LIFETIME = timedelta(minutes=15)


def check_upload_url(url: str, *, container: str, blob_name: str, issued_by: datetime) -> None:
    """`url` writes only `container/blob_name`, and expires within the limit of its issue, which
    happened no later than `issued_by` (take the time after the response arrives)."""
    query = _check_one_blob(url, container, blob_name)
    permissions = set("".join(query.get("sp", [])))
    check(
        "ACC-8",
        permissions and permissions <= UPLOAD_PERMISSIONS,
        f"upload URL grants {sorted(permissions)}",
    )
    _check_expiry(query, issued_by + UPLOAD_LIFETIME, issued_by)


def check_read_url(
    url: str, *, container: str, blob_name: str, media_type: str, issued_by: datetime
) -> None:
    """`url` only reads `container/blob_name`, has storage serve it as a `media_type` download,
    and expires within the limit of its issue, as for `check_upload_url`."""
    query = _check_one_blob(url, container, blob_name)
    check("ACC-8", query.get("sp") == ["r"], f"read URL grants {query.get('sp')}")
    check("ACC-8", query.get("rsct") == [media_type], f"served as {query.get('rsct')}")
    disposition = query.get("rscd", [""])[0]
    check("ACC-8", disposition.startswith("attachment;"), f"served as {disposition!r}")
    _check_expiry(query, issued_by + READ_LIFETIME, issued_by)


def _check_one_blob(url: str, container: str, blob_name: str) -> dict[str, list[str]]:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    path = unquote(parts.path)
    check("ACC-8", path.endswith(f"/{container}/{blob_name}"), f"URL path {path} != {blob_name}")
    check("ACC-8", query.get("sr") == ["b"], f"URL scope is {query.get('sr')}, not one blob")
    return query


def _check_expiry(query: dict[str, list[str]], latest: datetime, issued_by: datetime) -> None:
    expiry = datetime.fromisoformat(query["se"][0])
    check("ACC-8", expiry <= latest, f"URL expires {expiry - issued_by} after issue")
