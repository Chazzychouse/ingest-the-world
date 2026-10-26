"""Applies `migrations/*.sql` in filename order, once each, recording them in
`public.schema_migrations`. pgSTAC brings its own migrator (pypgstac) from M1.

Usage: python -m ingest_api.migrate [--seed]

The demo's items are real files, uploaded through the running stack by `make demo`.
"""

import argparse
from pathlib import Path
from typing import LiteralString, cast

import psycopg

from ingest_api.settings import get_settings

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"
DEV_SEED = ROOT / "seed" / "dev.sql"


def sql_file(path: Path) -> LiteralString:
    """A SQL file from this repository, as a query. Repo files are code, not user input."""
    return cast(LiteralString, path.read_text())


def migrate(database_url: str, *, seed: bool = False) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # Serializes concurrent runners, e.g. several API replicas starting at once.
        conn.execute("SELECT pg_advisory_lock(hashtext('ingest.schema_migrations'))")
        try:
            done = {row[0] for row in conn.execute("SELECT name FROM public.schema_migrations")}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(sql_file(path))
                    conn.execute(
                        "INSERT INTO public.schema_migrations (name) VALUES (%s)", (path.name,)
                    )
                applied.append(path.name)
            if seed:
                with conn.transaction():
                    conn.execute(sql_file(DEV_SEED))
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('ingest.schema_migrations'))")
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true", help="load development seed data")
    args = parser.parse_args()
    applied = migrate(get_settings().database_url, seed=args.seed)
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or '-'}")
    if args.seed:
        print("seed data loaded")


if __name__ == "__main__":
    main()
