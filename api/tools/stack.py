"""The running local stack (`make up`), driven from outside as a user and their browser would:
by the end-to-end suite (`tests/e2e/`) and the demo loader (`tools/demo.py`).

Sign-in uses Keycloak's password grant on the SPA's client, with the realm file's users. Bytes
go straight to Blob Storage through the signed URL, in blocks (Put Block, then Put Block List),
which is what the web upload engine does and what fires Event Grid's BlobCreated in the cloud.
"""

import base64
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# The users the stack signs in, with their passwords and roles; tests read them from here too.
REALM = Path(__file__).resolve().parents[2] / "infra" / "keycloak" / "ingest-realm.json"
API_URL = os.environ.get("INGEST_E2E_API", "http://localhost:8000")
# The web app's origin, through nginx, as a browser reaches the API (`/api/...`).
WEB_URL = os.environ.get("INGEST_E2E_WEB", "http://localhost:8081")
TOKEN_URL = os.environ.get(
    "INGEST_E2E_TOKEN_URL", "http://localhost:8080/realms/ingest/protocol/openid-connect/token"
)
WEB_CLIENT = "ingest-web"
# The Blob service REST version the browser's requests name.
BLOB_API_VERSION = "2025-01-05"
BLOCK_BYTES = 8 * 1024 * 1024
# Put Block List takes at most 50,000 blocks: 390 GiB at this block size.
MAX_BLOCKS = 50_000
TIMEOUT = httpx.Timeout(60.0)
# Polling for an upload's progress: a floor for small files, plus time for the worker to copy
# and hash.
POLL_SECONDS = 1.0
BASE_WAIT_SECONDS = 60.0
WAIT_BYTES_PER_SECOND = 20 * 1024 * 1024


def password_of(username: str) -> str:
    """The user's password, from the realm file the stack imports."""
    realm = json.loads(REALM.read_text())
    [user] = [u for u in realm["users"] if u["username"] == username]
    [credential] = [c for c in user["credentials"] if c["type"] == "password"]
    return credential["value"]


def sign_in(username: str) -> dict[str, str]:
    """Bearer headers for a realm user."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": WEB_CLIENT,
            "username": username,
            "password": password_of(username),
            "scope": "openid",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@dataclass(frozen=True, slots=True)
class Api:
    client: httpx.Client
    headers: dict[str, str]

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(path, json=body, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.put(path, json=body, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def get(self, path: str) -> dict[str, Any]:
        found = self.find(path)
        if found is None:
            raise LookupError(f"{path} not found")
        return found

    def find(self, path: str) -> dict[str, Any] | None:
        """The resource, or None if the API says it doesn't exist (or isn't ours to see)."""
        response = self.client.get(path, headers=self.headers)
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        response.raise_for_status()
        return response.json()


def _blocks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as file:
        for _ in range(MAX_BLOCKS):
            block = file.read(BLOCK_BYTES)
            if not block:
                return
            yield block
    raise ValueError(f"{path} needs more than {MAX_BLOCKS} blocks")


def _block_id(index: int) -> str:
    # Every id in a blob has the same length before encoding.
    return base64.b64encode(f"{index:08d}".encode()).decode()


def upload_blocks(client: httpx.Client, signed_url: str, path: Path) -> None:
    """Stages the file in blocks through the signed URL, then commits them in order."""
    headers = {"x-ms-version": BLOB_API_VERSION}
    ids: list[str] = []
    for index, block in enumerate(_blocks(path)):
        ids.append(_block_id(index))
        # Appended, not passed as `params`, which would replace the signature's query.
        staged = client.put(
            f"{signed_url}&comp=block&blockid={quote(ids[-1], safe='')}",
            content=block,
            headers=headers,
        )
        staged.raise_for_status()
    body = "".join(f"<Latest>{i}</Latest>" for i in ids)
    committed = client.put(
        f"{signed_url}&comp=blocklist",
        content=f'<?xml version="1.0" encoding="utf-8"?><BlockList>{body}</BlockList>',
        headers={**headers, "Content-Type": "application/xml"},
    )
    committed.raise_for_status()


def start_upload(api: Api, body: dict[str, Any], path: Path) -> str:
    """Opens an upload session for `body`, sends the file's bytes through it, and returns its id."""
    session = api.post("/uploads", body)
    with httpx.Client(timeout=TIMEOUT) as storage:
        upload_blocks(storage, session["upload_url"], path)
    return session["upload"]["id"]


def await_upload(
    api: Api, upload_id: str, size: int, *, done: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """The upload once `done` says so, polled for as long as a file of `size` may take."""
    deadline = time.monotonic() + BASE_WAIT_SECONDS + size / WAIT_BYTES_PER_SECOND
    polls = int((deadline - time.monotonic()) / POLL_SECONDS) + 1
    for _ in range(polls):
        upload = api.get(f"/uploads/{upload_id}")
        if done(upload):
            return upload
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"upload {upload_id} not done after {polls} polls")
