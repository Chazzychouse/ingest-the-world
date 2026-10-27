"""The soak: the simulators and property tests, round after round with fresh random seeds, until a
time budget is spent or a round fails.

    uv run python -m tools.soak --minutes=40                 # what nightly runs
    uv run python -m tools.soak --minutes=10 --plant=NAME    # canary: must catch a planted bug
    uv run python -m tools.soak --minutes=10 --plant=auto    # the canary of the day

A fixed number of examples made the soak's length a guess, and a guess past the job's timeout
cancelled it with nothing reported. Rounds instead use whatever time there is: one starts only
when there is time left for another as long as the longest so far, so the run ends on its own and
reports every seed it tried. A failing round stops the run; its report names the failing test and
the seed, and Hypothesis's database (uploaded by nightly) replays the shrunk example first.

The canary plants a known bug (a mutant from `mutants.py`) in a copy of the tree and passes only
if the soak's random rounds catch it with the mutant's invariant: a soak that finds nothing proves
something only while it can be shown to find a bug that is there.

Writes `reports/soak.json`, and on GitHub Actions a job summary.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tools.mutants import MUTANTS, REPO, SKIP, Mutant, plant

TESTS = ("tests/sim", "tests/test_access_model.py")
REPORTS = REPO / "api" / "reports"
MAX_ROUNDS = 500  # a bound on the loop; the budget ends a run long before it
# Time a round may run past its estimate before it's stopped: the job's timeout must stay ahead.
OVERRUN_SECONDS = 300
# Mutants the simulators catch from random inputs, one per family of invariants; `--plant=auto`
# takes one a day, so a week covers access, paging, search, the pipeline and the derive stage.
CANARIES = (
    "admin-bypasses-clearance",
    "cursor-repeats-last-item",
    "bbox-wraps-the-wrong-way",
    "redelivery-decides-again",
    "landing-cleared-without-etag",
    "give-up-orphans-a-written-derivative",
    "upload-list-repeats-page-edge",
)


@dataclass(frozen=True, slots=True)
class Round:
    seed: int
    seconds: float
    ok: bool
    invariants: list[str]  # broken, when the round failed
    failures: list[dict[str, Any]]  # the invariants report's, JSON


@dataclass(slots=True)
class Soak:
    minutes: float
    planted: str | None
    rounds: list[Round] = field(default_factory=list[Round])

    @property
    def failed(self) -> Round | None:
        return next((r for r in self.rounds if not r.ok), None)


def run_round(tree: Path, seed: int, *, timeout: float) -> Round:
    """One pass over the soak's tests in `tree`, every example drawn from `seed`."""
    report = Path(tempfile.mkdtemp()) / "round.json"
    started = time.monotonic()
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-x",
                "-n",
                "auto",
                "-p",
                "no:cacheprovider",
                f"--hypothesis-seed={seed}",
                f"--invariants-report={report}",
                *TESTS,
            ],
            cwd=tree / "api",
            capture_output=True,
            check=False,
            timeout=timeout,
            env={**os.environ, "INGEST_HYPOTHESIS_PROFILE": "soak"},
        )
    except subprocess.TimeoutExpired:
        return Round(
            seed,
            time.monotonic() - started,
            ok=False,
            invariants=[],
            failures=[{"error": f"the round ran past {timeout:.0f} s", "repro": _repro(seed)}],
        )
    seconds = time.monotonic() - started
    if not report.exists():
        return Round(
            seed,
            seconds,
            ok=False,
            invariants=[],
            failures=[
                {"error": "pytest wrote no report (collection error?)", "repro": _repro(seed)}
            ],
        )
    result = json.loads(report.read_text())
    failures: list[dict[str, Any]] = result["failures"]
    broken = sorted({str(i) for f in failures for i in f["invariants"]})
    return Round(seed, seconds, ok=bool(result["ok"]), invariants=broken, failures=failures)


def _repro(seed: int) -> str:
    return f"make sim SEED={seed} PROFILE=soak"


def soak(tree: Path, minutes: float, *, planted: str | None) -> Soak:
    run = Soak(minutes, planted)
    deadline = time.monotonic() + minutes * 60
    longest = 0.0
    for _ in range(MAX_ROUNDS):
        left = deadline - time.monotonic()
        if left < longest or left <= 0:
            break
        done = run_round(tree, secrets.randbelow(2**32), timeout=left + OVERRUN_SECONDS)
        run.rounds.append(done)
        longest = max(longest, done.seconds)
        print(
            f"round {len(run.rounds)}: seed {done.seed}, {done.seconds:.0f} s, "
            f"{'ok' if done.ok else 'FAILED ' + ', '.join(done.invariants)}",
            flush=True,
        )
        if not done.ok:
            break
    return run


def canary(name: str, today: datetime.date) -> Mutant:
    wanted = CANARIES[today.toordinal() % len(CANARIES)] if name == "auto" else name
    found = [m for m in MUTANTS if m.name == wanted]
    if not found:
        raise SystemExit(f"no mutant named {wanted}")
    return found[0]


def verdict(run: Soak, mutant: Mutant | None) -> tuple[bool, str]:
    failed = run.failed
    if mutant is None:
        if failed is None:
            return True, f"{len(run.rounds)} rounds passed"
        return False, f"round {run.rounds.index(failed) + 1} failed: seed {failed.seed}"
    if failed is None:
        return False, f"the planted {mutant.name} SURVIVED {len(run.rounds)} rounds"
    hit = sorted(set(failed.invariants) & set(mutant.caught_by))
    if not hit:
        return False, f"{mutant.name} caught as {failed.invariants}, not {list(mutant.caught_by)}"
    return True, f"{mutant.name} caught by {', '.join(hit)} in round {len(run.rounds)}"


def summary(run: Soak, message: str, *, ok: bool) -> str:
    title = f"canary: {run.planted}" if run.planted else "soak"
    lines = [
        f"## {title}: {'passed' if ok else 'FAILED'}",
        "",
        message,
        "",
        "| round | seed | seconds | result |",
        "|---|---|---|---|",
    ]
    for n, r in enumerate(run.rounds, 1):
        result = "ok" if r.ok else "failed: " + (", ".join(r.invariants) or "-")
        lines.append(f"| {n} | {r.seed} | {r.seconds:.0f} | {result} |")
    for f in run.failed.failures if run.failed else []:
        lines += [
            "",
            f"- `{f.get('test', '?')}`: {f.get('error', '')}",
            f"  `{f.get('repro', '')}`",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    minutes = next((float(a.split("=", 1)[1]) for a in argv if a.startswith("--minutes=")), 10.0)
    name = next((a.split("=", 1)[1] for a in argv if a.startswith("--plant=")), None)
    mutant = canary(name, datetime.datetime.now(datetime.UTC).date()) if name else None
    with tempfile.TemporaryDirectory() as tmp:
        tree = REPO
        if mutant is not None:  # never in the working tree: a copy of it, with the bug planted
            tree = Path(tmp) / "repo"
            shutil.copytree(REPO, tree, ignore=SKIP)
            if not plant(mutant, tree):
                raise SystemExit(f"{mutant.name}: pattern not found exactly once in {mutant.file}")
        run = soak(tree, minutes, planted=mutant.name if mutant else None)
    ok, message = verdict(run, mutant)
    REPORTS.mkdir(exist_ok=True)
    name_ = "canary" if mutant else "soak"
    (REPORTS / f"{name_}.json").write_text(
        json.dumps({"ok": ok, "message": message, **asdict(run)}, indent=2) + "\n"
    )
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(step_summary).open("a") as out:
            out.write(summary(run, message, ok=ok))
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
