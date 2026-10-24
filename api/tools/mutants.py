"""Plants known bugs one at a time and checks the suite catches each with the right invariant.

A green suite only means something if it goes red when the code is wrong. Each mutant here
is a realistic mistake; the run fails if any mutant survives, or is caught for the wrong
reason. Add a mutant whenever you add an invariant or fix a bug that tests missed.

    uv run python tools/mutants.py            # all mutants
    uv run python tools/mutants.py admin      # mutants whose name contains "admin"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

API = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutant:
    name: str
    file: str
    old: str
    new: str
    caught_by: tuple[str, ...]  # the run must fail at least one of these invariants


LIST_AUDIT = (
    "    audit.record(\n        conn,\n        ctx=ctx,\n        principal=principal,\n"
    '        action="collection.list",'
)

MUTANTS = [
    Mutant(
        "admin-bypasses-clearance",
        "migrations/0001_foundation.sql",
        "    WHERE m.rank <= authz.clearance_rank(clearances)\n      AND (",
        "    WHERE (m.rank <= authz.clearance_rank(clearances) OR 'Admin' = ANY (roles))\n"
        "      AND (",
        # Shows up as reading CUI (ACC-1) or granting on it (ACC-4), whichever runs first.
        ("ACC-1", "ACC-4"),
    ),
    Mutant(
        "group-grant-matches-roles",
        "migrations/0001_foundation.sql",
        "(g.principal_type = 'group' AND g.principal = ANY (groups))",
        "(g.principal_type = 'group' AND g.principal = ANY (roles || groups))",
        ("ACC-1",),
    ),
    Mutant(
        "no-role-hierarchy",
        "ingest_api/auth.py",
        "return self.roles | {role for role, rank in ROLE_RANK.items() if rank <= top}",
        "return self.roles",
        ("ACC-1",),
    ),
    Mutant(
        "forbidden-reveals-existence",
        "ingest_api/routes/collections.py",
        "    raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)",
        "    raise HTTPException(\n"
        "        status.HTTP_403_FORBIDDEN if actual else status.HTTP_404_NOT_FOUND, NOT_FOUND\n"
        "    )",
        ("ACC-2",),
    ),
    Mutant(
        "list-not-audited",
        "ingest_api/routes/collections.py",
        LIST_AUDIT,
        LIST_AUDIT.replace("audit.record(", "(lambda *_, **__: None)("),
        ("ACC-5",),
    ),
    Mutant(
        "create-skips-clearance",
        "ingest_api/routes/collections.py",
        '    if not cleared["ok"]:',
        "    if False:",
        ("ACC-4",),
    ),
    Mutant(
        "grant-skips-readability",
        "ingest_api/routes/collections.py",
        '    row = _readable_or_404(conn, collection_id, principal, ctx, "grant.put")',
        '    row = conn.execute("SELECT marking FROM catalog.collections WHERE id = %s",'
        " (collection_id,)).fetchone() or {}",
        ("ACC-4",),
    ),
    Mutant(
        "denial-audit-omits-marking",
        "ingest_api/routes/collections.py",
        '        marking=actual["marking"] if actual else None,',
        "        marking=None,",
        ("ACC-5",),
    ),
    Mutant(
        "nul-reaches-postgres",
        "ingest_api/routes/collections.py",
        "title: str = Field(min_length=1, max_length=200, pattern=NO_NUL)",
        "title: str = Field(min_length=1, max_length=200)",
        ("ROB-1",),
    ),
    Mutant(
        "audit-can-be-deleted",
        "migrations/0001_foundation.sql",
        "BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.events",
        "BEFORE UPDATE ON audit.events",
        ("ACC-6",),
    ),
]


def run(mutant: Mutant) -> tuple[bool, str]:
    path = API / mutant.file
    original = path.read_text()
    if original.count(mutant.old) != 1:
        return False, f"pattern not found exactly once in {mutant.file} (update the mutant)"
    path.write_text(original.replace(mutant.old, mutant.new))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-x",
                    "-p",
                    "no:cacheprovider",
                    f"--invariants-report={report}",
                ],
                cwd=API,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "INGEST_HYPOTHESIS_PROFILE": "mutants"},
            )
            if not report.exists():
                return False, "pytest produced no report (collection error?)"
            failures = json.loads(report.read_text())["failures"]
    finally:
        path.write_text(original)
    if not failures:
        return False, "SURVIVED: the suite passed with the bug in place"
    broken = {i for f in failures for i in f["invariants"]}
    hit = sorted(broken & set(mutant.caught_by))
    if not hit:
        expected = "/".join(mutant.caught_by)
        return False, f"caught, but as {sorted(broken) or 'no invariant'}, not {expected}"
    return True, f"caught by {', '.join(hit)} in {failures[0]['test']}"


def main(argv: list[str]) -> int:
    selected = [m for m in MUTANTS if not argv or any(a in m.name for a in argv)]
    survived = 0
    for mutant in selected:
        ok, message = run(mutant)
        survived += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {mutant.name:<30} {message}", flush=True)
    print(f"\n{len(selected) - survived}/{len(selected)} mutants caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
