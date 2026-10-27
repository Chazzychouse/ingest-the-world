"""`.3tz` archives: what the worker writes, the tiler reads back a file at a time, every file
exactly, and archives that aren't what they claim are refused before anything is served."""

import zipfile
from functools import partial
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ingest_api import tilesets
from ingest_api.tilesets import INDEX, Malformed, Writer, locate, read_index

names = st.lists(
    st.from_regex(r"[a-z]{1,8}(/[a-z0-9]{1,8}){0,3}\.(glb|json|bin)", fullmatch=True),
    min_size=1,
    max_size=40,
    unique=True,
)


def _reader(data: bytes) -> tilesets.Read:
    return partial(_slice, data)


def _slice(data: bytes, offset: int, length: int) -> bytes:
    return data[offset : offset + length]


def write(path: Path, files: dict[str, bytes]) -> bytes:
    with Writer(path) as out:
        for name, content in files.items():
            out.add(name, content)
    return path.read_bytes()


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(paths=names, data=st.data())
def test_every_file_reads_back_exactly_and_nothing_else_is_found(tmp_path, paths, data):
    files = {p: data.draw(st.binary(max_size=300)) for p in paths}
    archive = write(tmp_path / "t.3tz", files)
    read, size = _reader(archive), len(archive)

    index = read_index(read, size)

    assert len(index) == len(files)
    for name, content in files.items():
        found = locate(index, read, size, name)
        assert found is not None and read(*found) == content
    assert locate(index, read, size, "not/there.glb") is None


def test_the_index_is_the_last_entry_and_sorted_as_the_spec_says(tmp_path):
    archive = tmp_path / "t.3tz"
    write(archive, {f"tiles/{n}.glb": bytes([n]) for n in range(50)})

    with zipfile.ZipFile(archive) as z:
        last = z.infolist()[-1]
        index = z.read(INDEX)
    keys = [tilesets.sort_key(index[i : i + 16]) for i in range(0, len(index), 24)]
    assert last.filename == INDEX and last.compress_type == zipfile.ZIP_STORED
    assert keys == sorted(keys) and len(keys) == 50


def test_the_same_files_make_the_same_archive(tmp_path):
    files = {"tileset.json": b"{}", "tiles/0.glb": b"glTF"}

    assert write(tmp_path / "a.3tz", files) == write(tmp_path / "b.3tz", files)


def _garbage(_: bytes) -> bytes:
    return b"not a zip at all"


def _cut_short(archive: bytes) -> bytes:  # no end record
    return archive[: len(archive) // 2]


def _no_index(archive: bytes) -> bytes:
    return archive.replace(b"@3dtilesIndex1@", b"@3dtilesIndex9@")


def _no_local_headers(archive: bytes) -> bytes:
    return archive.replace(b"PK\x03\x04", b"PK\x00\x00")


@pytest.mark.parametrize("damage", [_garbage, _cut_short, _no_index, _no_local_headers])
def test_a_damaged_archive_is_malformed(tmp_path, damage):
    archive = damage(write(tmp_path / "t.3tz", {"tileset.json": b"{}", "tiles/0.glb": b"x" * 99}))

    with pytest.raises(Malformed):
        index = read_index(_reader(archive), len(archive))
        locate(index, _reader(archive), len(archive), "tileset.json")


def test_a_compressed_index_is_refused(tmp_path):
    path = tmp_path / "t.3tz"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("tileset.json", b"{}")
        z.writestr(INDEX, b"\0" * 24, compress_type=zipfile.ZIP_DEFLATED)
    archive = path.read_bytes()

    with pytest.raises(Malformed, match="uncompressed index"):
        read_index(_reader(archive), len(archive))
