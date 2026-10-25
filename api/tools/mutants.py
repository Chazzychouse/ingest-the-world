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
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutant:
    name: str
    file: str
    old: str
    new: str
    caught_by: tuple[str, ...]  # the run must fail at least one of these invariants


LIST_AUDIT = "    auditor.allowed(\n        AuditAction.COLLECTION_LIST,"
PUT_GRANT_CHECK = (
    "row = readable_or_404(conn, auditor, action, collection_id, principal)\n    store.put_grant("
)
ROUTES = "ingest_api/collections/routes.py"
UPLOAD_ROUTES = "ingest_api/uploads/routes.py"
# The latest definition of the access functions; earlier migrations' versions are replaced.
UPLOADS_SQL = "migrations/0003_uploads.sql"
ITEMS_SQL = "migrations/0004_items_and_jobs.sql"
PIPELINE = "ingest_api/worker/pipeline.py"
READ_GRANTED = (
    "      AND c.id IN (SELECT authz.granted_collections(subject, roles, groups, 'read'))"
)

MUTANTS = [
    Mutant(
        "admin-bypasses-clearance",
        UPLOADS_SQL,
        f"    WHERE authz.cleared_for(c.marking, clearances)\n{READ_GRANTED}",
        f"    WHERE (authz.cleared_for(c.marking, clearances) OR 'Admin' = ANY (roles))\n"
        f"{READ_GRANTED}",
        # Shows up as reading CUI (ACC-1) or granting on it (ACC-4), whichever runs first.
        ("ACC-1", "ACC-4"),
    ),
    Mutant(
        "group-grant-matches-roles",
        UPLOADS_SQL,
        "(g.principal_type = 'group' AND g.principal = ANY (groups))",
        "(g.principal_type = 'group' AND g.principal = ANY (roles || groups))",
        ("ACC-1",),
    ),
    Mutant(
        "no-role-hierarchy",
        "ingest_api/auth.py",
        "return self.roles | {role for role, rank in ROLE_RANK.items() if rank <= top}",
        "return self.roles",
        # Breaks reading (a grant to Reader stops covering a Curator) and writing (a Curator
        # stops counting as a Contributor); the write check often fails first.
        ("ACC-1", "ACC-7"),
    ),
    Mutant(
        "forbidden-reveals-existence",
        ROUTES,
        "status_code=status.HTTP_404_NOT_FOUND,",
        "status_code=403 if store.marking_of(conn, collection_id) else 404,",
        ("ACC-2",),
    ),
    Mutant(
        "list-not-audited",
        ROUTES,
        LIST_AUDIT,
        LIST_AUDIT.replace("auditor.allowed(", "(lambda *_, **__: None)("),
        ("ACC-5",),
    ),
    Mutant(
        "create-skips-clearance",
        ROUTES,
        "    if not cleared:",
        "    if False:",
        ("ACC-4",),
    ),
    Mutant(
        "grant-skips-readability",
        ROUTES,
        PUT_GRANT_CHECK,
        PUT_GRANT_CHECK.replace(
            "principal)",
            '__import__("dataclasses").replace(principal, clearances=frozenset({"CUI"})))',
        ),
        ("ACC-4",),
    ),
    Mutant(
        "denial-audit-omits-marking",
        ROUTES,
        "        marking=store.marking_of(conn, collection_id),",
        "        marking=None,",
        ("ACC-5",),
    ),
    Mutant(
        "nul-reaches-postgres",
        "ingest_api/collections/models.py",
        "title: str = Field(min_length=1, max_length=200, pattern=NO_NUL_PATTERN)",
        "title: str = Field(min_length=1, max_length=200)",
        ("ROB-1",),
    ),
    Mutant(
        "read-grant-allows-upload",
        UPLOADS_SQL,
        "granted_collections(subject, roles, groups, 'write')",
        "granted_collections(subject, roles, groups, 'read')",
        ("ACC-7",),
    ),
    Mutant(
        "upload-skips-role-check",
        UPLOADS_SQL,
        "      AND 'Contributor' = ANY (roles)\n",
        "",
        ("ACC-7",),
    ),
    Mutant(
        "anyone-renews-an-upload",
        UPLOAD_ROUTES,
        "    if row.created_by != principal.subject:",
        "    if False:",
        ("ACC-7",),
    ),
    Mutant(
        "upload-url-can-read",
        "ingest_api/adapters/azure_blob.py",
        "BlobSasPermissions(create=True, write=True)",
        "BlobSasPermissions(read=True, create=True, write=True)",
        ("ACC-8",),
    ),
    Mutant(
        # Found on the running stack, not by the suite: Compose names Azurite by host, the SDK
        # then reads the account name from the host, and storage refused every signed URL.
        "sas-signed-for-sdk-parsed-account",
        "ingest_api/adapters/azure_blob.py",
        '"account_name": account_name(account_url),',
        '"account_name": BlobServiceClient(account_url).account_name or "",',
        ("ACC-8",),
    ),
    Mutant(
        "upload-url-outlives-limit",
        UPLOAD_ROUTES,
        "return timedelta(minutes=runtime.settings.upload_url_minutes)",
        "return timedelta(days=1)",
        ("ACC-8",),
    ),
    Mutant(
        "audit-can-be-deleted",
        "migrations/0001_foundation.sql",
        "BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.events",
        "BEFORE UPDATE ON audit.events",
        ("ACC-6",),
    ),
    Mutant(
        "landing-cleared-without-etag",
        PIPELINE,
        "store.delete(LANDING, event.name, if_match=event.etag)",
        "store.delete(LANDING, event.name)",
        # Finishing an old version deletes a newer one that landed meanwhile: it is never seen.
        ("ING-2",),
    ),
    Mutant(
        "redelivery-decides-again",
        PIPELINE,
        'outcome = job.stage if job.stage != "received" else decide(conn, p, event, upload)',
        "outcome = decide(conn, p, event, upload)",
        # The duplicate finds its upload closed, "rejects" itself and discards the item's raw.
        ("ING-1", "ING-3", "ING-5"),
    ),
    Mutant(
        "scan-ignored",
        PIPELINE,
        "    if not verdict.clean:\n",
        "    if verdict.clean and not verdict.clean:\n",
        ("ING-4",),
    ),
    Mutant(
        "checksum-not-verified",
        PIPELINE,
        "if upload.sha256 is not None and sha256 != upload.sha256:",
        "if False:",
        ("ING-2",),
    ),
    Mutant(
        "rejection-keeps-raw-copy",
        PIPELINE,
        "    if raw is not None and raw.metadata.get(SOURCE_ETAG) == etag:\n",
        "    if False:\n",
        ("ING-5",),
    ),
    Mutant(
        "promote-before-scan",
        PIPELINE,
        "    verdict = p.scanner.scan(LANDING, event.name)\n",
        "    _copy_to_raw(p.store, event)\n    verdict = p.scanner.scan(LANDING, event.name)\n",
        ("ING-4", "ING-5"),
    ),
    Mutant(
        "job-outcome-can-change",
        ITEMS_SQL,
        "    IF OLD.stage <> 'received' AND NEW.stage IS DISTINCT FROM OLD.stage THEN",
        "    IF false THEN",
        ("ING-2",),
    ),
    Mutant(
        "item-marking-floor-dropped",
        ITEMS_SQL,
        "    IF (SELECT rank FROM authz.markings WHERE code = NEW.marking) < (",
        "    IF false AND (SELECT rank FROM authz.markings WHERE code = NEW.marking) < (",
        ("ACC-3",),
    ),
    Mutant(
        # Slice 3-4 review: a reader that crashed natively took the worker down on every
        # delivery, and its version never reached an outcome.
        "reader-crash-escapes-sandbox",
        "ingest_api/adapters/process_sandbox.py",
        "            return EXITED\n",
        '            raise RuntimeError("the extraction child died")\n',
        ("ING-8",),
    ),
    Mutant(
        "last-delivery-not-given-up",
        "ingest_api/worker/runner.py",
        "        if message.delivery_count >= retry.max_deliveries:\n",
        "        if False:\n",
        ("ING-2",),
    ),
    Mutant(
        "dead-letters-dropped-by-sweep",
        "ingest_api/worker/runner.py",
        "    return _give_up(connect, pipeline, event, jobs.Reason.DEAD_LETTERED)\n",
        "    return Settlement.COMPLETE\n",
        ("ING-2",),
    ),
    Mutant(
        "message-locks-not-renewed",
        "ingest_api/adapters/service_bus.py",
        "        if self._renewal > 0:\n",
        "        if False:\n",
        ("ING-10",),
    ),
    Mutant(
        "pipeline-outcome-not-audited",
        PIPELINE,
        "    if not audit:\n",
        "    if True:\n",
        ("ACC-5",),
    ),
    Mutant(
        "duplicates-not-rejected",
        "migrations/0007_dedup.sql",
        "CREATE UNIQUE INDEX items_one_per_content ON catalog.items (collection_id, sha256)",
        "CREATE INDEX items_one_per_content ON catalog.items (collection_id, sha256)",
        ("ING-9",),
    ),
    Mutant(
        "extraction-ignores-budget",
        "ingest_api/worker/extract.py",
        "        if self.fetched + length > MAX_EXTRACT_BYTES:",
        "        if False and self.fetched + length > MAX_EXTRACT_BYTES:",
        ("ING-6",),
    ),
]


# Copied into each mutant's sandbox; everything else is rebuildable or irrelevant to tests.
SKIP = shutil.ignore_patterns(
    ".git",
    ".venv",
    "node_modules",
    "dist",
    ".hypothesis",
    "reports",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
)


def run(mutant: Mutant, snapshot: Path) -> tuple[bool, str]:
    """Applies `mutant` to a throwaway copy of `snapshot` and runs the suite there."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "repo"
        shutil.copytree(snapshot, sandbox)
        path = sandbox / "api" / mutant.file
        original = path.read_text()
        if original.count(mutant.old) != 1:
            return False, f"pattern not found exactly once in {mutant.file} (update the mutant)"
        path.write_text(original.replace(mutant.old, mutant.new))
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
            cwd=sandbox / "api",
            capture_output=True,
            check=False,
            env={**os.environ, "INGEST_HYPOTHESIS_PROFILE": "mutants"},
        )
        if not report.exists():
            return False, "pytest produced no report (collection error?)"
        failures = json.loads(report.read_text())["failures"]
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
    # Every mutant runs against the tree as it was at the start, so editing code while
    # mutants run neither breaks the run nor leaks half-made changes into it.
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "repo"
        shutil.copytree(REPO, snapshot, ignore=SKIP)
        for mutant in selected:
            ok, message = run(mutant, snapshot)
            survived += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {mutant.name:<30} {message}", flush=True)
    print(f"\n{len(selected) - survived}/{len(selected)} mutants caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
