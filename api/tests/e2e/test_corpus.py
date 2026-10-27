"""Every sample in `samples/manifest.json`, uploaded through the running stack, ends as the
manifest says: the right outcome, and for cataloged files the right kind and metadata.

    make samples && make e2e       # INGEST_E2E_LARGE=1 adds the multi-GB samples,
                                   # INGEST_E2E_DEMO=1 the demo's

Each run makes its own collections (as alice), so reruns never meet an earlier run's files,
and uploads as `e2e`, a Contributor granted them by name and not by role, so the demo's
non-admin users never see them. The outcome is read through the API
as the web app will, and so is the item, which must also be found by searching for it (by its
footprint, where the manifest gives one) and download, through the link the API signs, as the
bytes uploaded. Once its derivatives settle it must have exactly those the manifest names, none
failed, each COG among them must pass rio-cogeo's validator, and each GeoParquet the GeoParquet
project's schema. Expectations live only in the
manifest.
"""

import hashlib
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from rio_cogeo.cogeo import cog_validate

from ingest_api.items.metadata import TILESET_ROLE
from ingest_api.items.routes import SHA256_MULTIHASH
from tests.geoparquet import valid_geoparquet
from tests.invariants import check
from tests.meshes import validated
from tools import samples
from tools.stack import API_URL, TIMEOUT, Api, await_upload, sign_in, start_upload

pytestmark = pytest.mark.e2e

CORPUS = samples.selected(
    samples.load(),
    large=bool(os.environ.get("INGEST_E2E_LARGE")),
    demo=bool(os.environ.get("INGEST_E2E_DEMO")),
)
# The suite's own user: its run collections are granted to it alone.
USER = "e2e"
WRONG_SHA256 = "0" * 64
PARQUET = "application/vnd.apache.parquet"


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API_URL, timeout=TIMEOUT) as client:
        yield client


@pytest.fixture(scope="module")
def collection(http: httpx.Client) -> Callable[[str], str]:
    """The run's collection for a label, made on first use, writable by the suite's user."""
    admin = Api(http, sign_in("alice"))
    subject = Api(http, sign_in(USER)).get("/me")["subject"]
    run = uuid.uuid4().hex[:8]
    made: dict[str, str] = {}

    def get(label: str) -> str:
        if label not in made:
            collection_id = f"e2e-{run}-{label}"
            admin.post("/collections", {"id": collection_id, "title": f"E2E {run} {label}"})
            admin.put(f"/collections/{collection_id}/grants/user/{subject}", {"access": "write"})
            made[label] = collection_id
        return made[label]

    return get


def _declared(sample: samples.Sample, upload: samples.Upload) -> str | None:
    match upload.declared_sha256:
        case "right":
            return sample.sha256
        case "wrong":
            return WRONG_SHA256
        case "none":
            return None


def _item(api: Api, upload_id: str) -> dict[str, Any] | None:
    """The item as the API serves it, in the manifest's terms."""
    stac = api.find(f"/items/{upload_id}")
    if stac is None:
        return None
    properties = stac["properties"]
    return {
        "kind": properties["ingest:kind"],
        "media_type": properties["ingest:media_type"],
        "sha256": properties["file:checksum"].removeprefix(SHA256_MULTIHASH),
        "size": properties["file:size"],
        "extraction": properties["ingest:extraction"],
        "crs": properties["proj:code"] or properties["proj:wkt2"],
        "properties": properties,
        "bbox": tuple(stac["bbox"]) if "bbox" in stac else None,
    }


def _found(api: Api, upload_id: str, expect: samples.Expect) -> bool:
    """Whether searching for the item finds it: by id, and inside its expected footprint."""
    body: dict[str, Any] = {"ids": [upload_id]}
    if expect.bbox is not None:
        body["bbox"] = list(expect.bbox)
    return [f["id"] for f in api.post("/search", body)["features"]] == [upload_id]


def _check_item(label: str, item: dict[str, Any], sample: samples.Sample, e: samples.Expect):
    check("ING-5", item["sha256"] == sample.sha256, f"{label}: checksum {item['sha256']}")
    for field in ("kind", "media_type", "extraction", "crs"):
        wanted = getattr(e, field)
        if wanted is not None:
            assert item[field] == wanted, f"{label}: {field} {item[field]!r}, wanted {wanted!r}"
    if e.bbox is not None:
        assert item["bbox"] is not None, f"{label}: no footprint, wanted {e.bbox}"
        off = max(abs(a - b) for a, b in zip(item["bbox"], e.bbox, strict=True))
        assert off <= e.bbox_tolerance, f"{label}: footprint {item['bbox']}, wanted {e.bbox}"
    for key, wanted in e.properties.items():
        assert item["properties"].get(key) == wanted, (
            f"{label}: {key} = {item['properties'].get(key)!r}, wanted {wanted!r}"
        )


def _download(api: Api, upload_id: str, role: str, *, into: Callable[[bytes], object]) -> None:
    """Streams one of the item's files from storage, through the link the API signs for it."""
    url = api.get(f"/items/{upload_id}/assets/{role}/url")["url"]
    with httpx.stream("GET", url, timeout=TIMEOUT) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            into(chunk)


def _downloaded_sha256(api: Api, upload_id: str) -> str:
    """The SHA-256 of the item's original, as downloaded."""
    digest = hashlib.sha256()
    _download(api, upload_id, "original", into=digest.update)
    return digest.hexdigest()


def _check_derivatives(api: Api, label: str, upload_id: str, e: samples.Expect, scratch: Path):
    """The item's derivatives, once settled, against the manifest's; each COG and tileset
    validated."""
    stac = api.get(f"/items/{upload_id}")
    made = {role: a["type"] for role, a in stac["assets"].items() if role != "original"}
    failed = stac["properties"]["ingest:derivative_failures"]
    assert failed == {}, f"{label}: derivatives failed: {failed}"
    if e.derivatives is None:
        return
    check("ING-12", made == e.derivatives, f"{label}: derivatives {made}, wanted {e.derivatives}")
    for role, asset in stac["assets"].items():
        if role == "original":
            continue
        path = scratch / f"{upload_id}-{role}"
        with path.open("wb") as out:
            _download(api, upload_id, role, into=out.write)
        if "cloud-optimized" in asset["roles"]:
            valid, errors, _ = cog_validate(path, quiet=True)
            assert valid, f"{label}: {role} is not a valid COG: {errors}"
        if asset["type"] == PARQUET:
            assert valid_geoparquet(path).num_rows > 0, f"{label}: {role} has no features"
        if role == TILESET_ROLE:
            report = validated(path.rename(path.with_suffix(".3tz")), scratch)
            problems = report["numErrors"] + report["numWarnings"]
            check("ING-14", problems == 0, f"{label}: the tileset has {problems} problems")


@pytest.mark.invariant(
    "ING-2", "ING-4", "ING-5", "ING-6", "ING-12", "ING-13", "ING-14", "SRCH-1", "ACC-9"
)
@pytest.mark.parametrize("sample", CORPUS, ids=[s.name for s in CORPUS])
def test_each_sample_reaches_its_outcome(sample: samples.Sample, http, collection, tmp_path):
    if not sample.path.exists():
        pytest.fail(f"{sample.path} is missing: run `make samples`")
    api = Api(http, sign_in(USER))
    for index, upload in enumerate(sample.uploads):  # in order: a duplicate follows its original
        label = f"{sample.name}[{index}]"
        body: dict[str, Any] = {
            "collection_id": collection(upload.collection),
            "filename": upload.filename or sample.name,
            "size_bytes": sample.size,
        }
        if declared := _declared(sample, upload):
            body["sha256"] = declared
        upload_id = start_upload(api, body, sample.path)

        outcome = await_upload(api, upload_id, sample.size, done=lambda u: u["status"] != "pending")
        status, expect = outcome["status"], upload.expect
        reason = outcome["signature"] if status == "quarantined" else outcome["reason"]
        check("ING-2", status == expect.outcome, f"{label}: {status}, wanted {expect.outcome}")
        if expect.reason is not None:
            assert reason == expect.reason, f"{label}: reason {reason!r}, wanted {expect.reason!r}"
        item = _item(api, upload_id)
        check("ING-4", (item is not None) == (status == "cataloged"), f"{label}: item {item}")
        if item is not None:
            _check_item(label, item, sample, expect)
            check("SRCH-1", _found(api, upload_id, expect), f"{label}: search didn't find it")
            downloaded = _downloaded_sha256(api, upload_id)
            check("ACC-9", downloaded == sample.sha256, f"{label}: downloaded {downloaded}")
            settled = await_upload(
                api, upload_id, sample.size, done=lambda u: u["stage"] != "deriving"
            )
            stage = settled["stage"]
            check("ING-13", stage == "ready", f"{label}: derivatives ended {stage}")
            derivatives = api.get(f"/items/{upload_id}")["properties"]["ingest:derivatives"]
            check("ING-13", derivatives == "ready", f"{label}: the item says {derivatives}")
            _check_derivatives(api, label, upload_id, expect, tmp_path)
