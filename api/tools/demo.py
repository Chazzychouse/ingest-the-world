"""The demo catalog (`samples/demo.json`), loaded into the running stack (`make up`).

    make demo           # fetch the files it needs, then upload what isn't there yet

Real files go through the real pipeline, so every item has its bytes, thumbnails, previews and
derivatives, as an upload from the web app would. Collections are made as alice, files uploaded
as carol; a file already cataloged in its collection is skipped, so a rerun fills only gaps.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from tools import samples
from tools.stack import API_URL, TIMEOUT, Api, await_upload, sign_in, start_upload

DEMO = samples.REPO / "samples" / "demo.json"
ADMIN, UPLOADER = "alice", "carol"
# Search's page limit; no demo collection holds more files than this.
PAGE = 100


@dataclass(frozen=True, slots=True)
class Placement:
    sample: samples.Sample
    collection: str
    tags: tuple[str, ...]


def _placements(raw: dict[str, Any], corpus: list[samples.Sample]) -> list[Placement]:
    by_name = {s.name: s for s in corpus}
    return [
        Placement(sample=by_name[p["sample"]], collection=p["collection"], tags=tuple(p["tags"]))
        for p in raw["items"]
    ]


def _ensure_collections(admin: Api, collections: list[dict[str, Any]]) -> None:
    """Makes each collection that doesn't exist yet, and grants it to its roles."""
    for c in collections:
        if admin.find(f"/collections/{c['id']}") is None:
            admin.post("/collections", {k: c[k] for k in ("id", "title", "description")})
            print(f"made     {c['id']}", flush=True)
        for role, access in c["grants"].items():
            admin.put(f"/collections/{c['id']}/grants/role/{role}", {"access": access})


def _cataloged(api: Api, collection: str) -> set[str]:
    """The filenames already cataloged in a collection."""
    found = api.post("/search", {"collections": [collection], "limit": PAGE})
    return {f["properties"]["title"] for f in found["features"]}


def _upload(api: Api, placement: Placement) -> str:
    """Uploads one file and waits for its outcome, then for its derivatives; the outcome."""
    sample = placement.sample
    body = {
        "collection_id": placement.collection,
        "filename": sample.name,
        "size_bytes": sample.size,
        "sha256": sample.sha256,
        "tags": list(placement.tags),
    }
    upload_id = start_upload(api, body, sample.path)
    try:
        outcome = await_upload(api, upload_id, sample.size, done=lambda u: u["status"] != "pending")
        if outcome["status"] != "cataloged":
            return outcome["status"]
        derived = await_upload(api, upload_id, sample.size, done=lambda u: u["stage"] != "deriving")
    except TimeoutError as exc:
        return str(exc)
    return f"cataloged, derivatives {derived['stage']}"


def load(raw: dict[str, Any], corpus: list[samples.Sample]) -> int:
    """Loads the demo into the stack; 1 if any file didn't catalog, else 0."""
    placements = _placements(raw, corpus)
    with httpx.Client(base_url=API_URL, timeout=TIMEOUT) as http:
        _ensure_collections(Api(http, sign_in(ADMIN)), raw["collections"])
        reader = Api(http, sign_in(UPLOADER))
        present = {c: _cataloged(reader, c) for c in {p.collection for p in placements}}
        failed = 0
        for placement in placements:
            name, collection = placement.sample.name, placement.collection
            if name in present[collection]:
                print(f"present  {collection}/{name}", flush=True)
                continue
            # Signed in afresh for each file: a token outlives a few uploads, not the whole demo.
            outcome = _upload(Api(http, sign_in(UPLOADER)), placement)
            failed += not outcome.startswith("cataloged")
            print(f"uploaded {collection}/{name}: {outcome}", flush=True)
    return 1 if failed else 0


def main(path: Path = DEMO) -> int:
    raw = cast(dict[str, Any], json.loads(path.read_text()))
    corpus = samples.load()
    needed = {p["sample"] for p in raw["items"]}
    if samples.fetch_all(corpus, [s for s in corpus if s.name in needed]):
        return 1
    return load(raw, corpus)


if __name__ == "__main__":
    sys.exit(main())
