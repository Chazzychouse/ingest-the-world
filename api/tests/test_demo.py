"""The demo catalog (`samples/demo.json`) names only files the manifest has and collections that
will exist, so `make demo` can't fail halfway on a typo. Its loading is `make e2e DEMO=1`'s and
`make demo`'s to prove, against the running stack."""

import json
from collections import Counter

from tools import samples
from tools.demo import DEMO

RAW = json.loads(DEMO.read_text())


def test_every_demo_file_is_a_fetched_sample_cataloged_by_the_e2e_suite():
    corpus = {s.name: s for s in samples.load()}
    for placement in RAW["items"]:
        sample = corpus.get(placement["sample"])
        assert sample is not None, f"{placement['sample']} is not in the manifest"
        assert sample.set in ("default", "demo"), f"{sample.name} is in the {sample.set} set"
        assert sample.uploads[0].expect.outcome == "cataloged", f"{sample.name} won't catalog"


def test_every_demo_collection_is_made_by_the_demo_or_the_dev_seed(db):
    seeded = {row[0] for row in db.execute("SELECT id FROM catalog.collections").fetchall()}
    made = {c["id"] for c in RAW["collections"]}
    assert not made & seeded, f"the demo remakes seeded collections: {made & seeded}"
    for placement in RAW["items"]:
        assert placement["collection"] in made | seeded, placement


def test_no_file_is_placed_twice_in_one_collection():
    # A second copy would be a duplicate (ING-9), and the loader would report it as a failure.
    placed = Counter((p["sample"], p["collection"]) for p in RAW["items"])
    assert [key for key, n in placed.items() if n > 1] == []
