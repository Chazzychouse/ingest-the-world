"""The demo seed still loads against the current schema, and loading it twice changes nothing."""

from collections.abc import Callable

import psycopg

from ingest_api.migrate import DEMO_SEED, sql_file


def _counts(conn: psycopg.Connection) -> tuple[int, int, int]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM catalog.collections), (SELECT count(*) FROM catalog.items),"
        " (SELECT count(*) FROM catalog.items WHERE footprint IS NOT NULL)"
    ).fetchone()
    assert row is not None
    return row


def test_demo_seed_loads_and_reloads(fresh_database: Callable[[], str]) -> None:
    with psycopg.connect(fresh_database(), autocommit=True) as conn:
        with conn.transaction():
            conn.execute(sql_file(DEMO_SEED))
        first = _counts(conn)
        with conn.transaction():
            conn.execute(sql_file(DEMO_SEED))
        assert _counts(conn) == first
    _, items, placed = first
    assert placed > 0
    assert items > placed  # some items have no footprint
