"""Narrowing parsed JSON from untrusted files into the shapes readers expect. Anything of the
wrong type reads as absent, so a malformed document yields less metadata, never an error."""

from typing import cast


def as_object(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def as_list(value: object) -> list[object] | None:
    return cast(list[object], value) if isinstance(value, list) else None


def as_number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def objects(parent: dict[str, object], key: str) -> list[dict[str, object]]:
    """The JSON objects in `parent[key]`, if it's a list; other entries are skipped."""
    value = parent.get(key)
    items = cast(list[object], value) if isinstance(value, list) else []
    return [o for o in map(as_object, items) if o is not None]


def integer(parent: dict[str, object], key: str) -> int | None:
    value = parent.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def numbers(parent: dict[str, object], key: str, length: int) -> list[float] | None:
    """`parent[key]` if it's a list of exactly `length` numbers."""
    value = parent.get(key)
    items = cast(list[object], value) if isinstance(value, list) else []
    found = [float(v) for v in items if isinstance(v, int | float) and not isinstance(v, bool)]
    return found if len(found) == length == len(items) else None


def strings(parent: dict[str, object], key: str, limit: int) -> list[str]:
    value = parent.get(key)
    items = cast(list[object], value) if isinstance(value, list) else []
    return [v for v in items if isinstance(v, str)][:limit]
