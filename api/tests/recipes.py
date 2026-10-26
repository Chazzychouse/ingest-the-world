"""Recipes for testing the derive stage without GDAL: small, fast, and each with one trait the
stage has to handle (deterministic or not, for some kinds only, refusing some originals)."""

import hashlib
import random
from collections.abc import Mapping
from pathlib import Path

from ingest_api.items.kinds import Kind
from ingest_api.worker.recipe import Source, Unrenderable

SOFTWARE: Mapping[str, str] = {"tests": "1"}


class Digest:
    """Every original's SHA-256, as text: deterministic."""

    role, version, media_type, suffix = "digest", 1, "text/plain", ".txt"
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        return True

    def render(self, original: Path, scratch: Path) -> Path:
        out = scratch / "digest.txt"
        out.write_text(hashlib.sha256(original.read_bytes()).hexdigest())
        return out


class DigestV2(Digest):
    """Digest's next version, in capitals: its derivatives live beside version 1's."""

    version = 2

    def render(self, original: Path, scratch: Path) -> Path:
        out = super().render(original, scratch)
        out.write_text(out.read_text().upper())
        return out


class Stamp:
    """A raster's first bytes after a random stamp: different on every render."""

    role, version, media_type, suffix = "stamp", 1, "application/octet-stream", ".bin"
    deterministic, software = False, SOFTWARE

    def __init__(self, rng: random.Random):
        self._rng = rng

    def applies(self, source: Source) -> bool:
        return source.kind == Kind.RASTER

    def render(self, original: Path, scratch: Path) -> Path:
        out = scratch / "stamp.bin"
        out.write_bytes(self._rng.randbytes(8) + original.read_bytes()[:16])
        return out


class Picky:
    """The original's size, unless it starts with `bad`, which it refuses."""

    role, version, media_type, suffix = "picky", 2, "text/plain", ".txt"
    deterministic, software = True, SOFTWARE

    def applies(self, source: Source) -> bool:
        return True

    def render(self, original: Path, scratch: Path) -> Path:
        content = original.read_bytes()
        if content.startswith(b"bad"):
            raise Unrenderable("starts badly")
        out = scratch / "picky.txt"
        out.write_text(str(len(content)))
        return out
