"""Access rules, asked of the database.

The rules live in SQL (`authz.cleared_for`, `authz.readable_collections`) so that every query,
whichever route issues it, filters through the same definition (ACC-1). This module only
passes a `Principal` to those functions; it never re-implements them.
"""

from dataclasses import dataclass

from psycopg import Connection
from psycopg.rows import TupleRow, class_row

from ingest_api.auth import Principal

# Use as `WHERE c.id IN ({READABLE_IDS})` with `principal_args(principal)` as parameters.
READABLE_IDS = "SELECT authz.readable_collections(%s, %s, %s, %s)"


@dataclass(frozen=True, slots=True)
class Marking:
    code: str
    label: str
    banner_color: str
    rank: int


def principal_args(principal: Principal) -> tuple[str, list[str], list[str], list[str]]:
    """The parameters for `READABLE_IDS`, in order."""
    return (
        principal.subject,
        sorted(principal.effective_roles),
        sorted(principal.groups),
        sorted(principal.clearances),
    )


def cleared_for(conn: Connection[TupleRow], marking: str, principal: Principal) -> bool | None:
    """Whether `principal` is cleared for `marking`; None if the marking doesn't exist."""
    row = conn.execute(
        "SELECT authz.cleared_for(%s, %s)", (marking, sorted(principal.clearances))
    ).fetchone()
    return None if row is None else row[0]


def highest_clearance(conn: Connection[TupleRow], principal: Principal) -> Marking:
    with conn.cursor(row_factory=class_row(Marking)) as cur:
        marking = cur.execute(
            """
            SELECT code, label, banner_color, rank FROM authz.markings
            WHERE rank = authz.clearance_rank(%s)
            """,
            (sorted(principal.clearances),),
        ).fetchone()
    if marking is None:
        # Migration 0001 creates the rank-0 baseline marking; without it nothing is safe.
        raise RuntimeError("authz.markings has no rank-0 baseline marking")
    return marking
