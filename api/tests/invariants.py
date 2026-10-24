"""Pytest plugin tying tests to the invariants in docs/spec/03-invariants.md.

- `@pytest.mark.invariant("ACC-1", ...)` declares which invariants a test enforces.
  Citing an ID the doc doesn't define is a collection error.
- `violated(id, message)` / `check(id, condition, message)` fail with the ID in the message,
  so a failure names the property it broke, not just the line.
- `--check-invariants` (used by `make check` and CI) also fails the run when an `enforced`
  invariant has no test, so the doc can't claim more than the suite checks.
- `--invariants-report PATH` writes a JSON summary meant for agents and dashboards: each
  invariant's tests and outcome, and for each failure the IDs broken, the error, and the
  command that reproduces it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "spec" / "03-invariants.md"
# Tests outside pytest (the web app's vitest suite) cite invariants as `[ID]` in a describe
# or test name. They run in their own CI job; here they only count towards traceability.
EXTERNAL_TESTS = [ROOT / "web" / "src"]
_HEADING = re.compile(r"^### ([A-Z]+-\d+) (.+)$")
_STATUS = re.compile(r"^Status: (\w+)")
_CITED = re.compile(r"\[([A-Z]+-\d+)\]")


class InvariantViolation(AssertionError):
    def __init__(self, invariant: str, message: str):
        super().__init__(f"[{invariant}] {message}")
        self.invariant = invariant


def violated(invariant: str, message: str) -> InvariantViolation:
    return InvariantViolation(invariant, message)


def check(invariant: str, condition: object, message: str) -> None:
    if not condition:
        raise InvariantViolation(invariant, message)


@dataclass
class Invariant:
    id: str
    title: str
    status: str
    tests: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def load(doc: Path = DOC) -> dict[str, Invariant]:
    registry: dict[str, Invariant] = {}
    current: Invariant | None = None
    for line in doc.read_text().splitlines():
        if heading := _HEADING.match(line):
            current = Invariant(heading[1], heading[2], status="")
            registry[current.id] = current
        elif current and not current.status and (status := _STATUS.match(line)):
            current.status = status[1]
    missing = [i.id for i in registry.values() if i.status not in ("enforced", "planned")]
    if missing:
        raise pytest.UsageError(f"{doc}: invariants without a valid Status line: {missing}")
    return registry


def pytest_addoption(parser):
    group = parser.getgroup("invariants")
    group.addoption(
        "--check-invariants",
        action="store_true",
        help="fail if an enforced invariant has no test",
    )
    group.addoption("--invariants-report", metavar="PATH", help="write a JSON report")


_config: pytest.Config | None = None


def pytest_configure(config):
    global _config
    config.addinivalue_line("markers", "invariant(*ids): invariants this test enforces")
    config._invariants = load()
    config._invariant_failures = []
    config._ran = set()
    _config = config


def pytest_collection_modifyitems(config, items):
    registry: dict[str, Invariant] = config._invariants
    unknown = []
    for item in items:
        for marker in item.iter_markers("invariant"):
            for invariant in marker.args:
                if invariant not in registry:
                    unknown.append(f"{item.nodeid}: {invariant}")
                else:
                    registry[invariant].tests.append(item.nodeid)
    for root in EXTERNAL_TESTS:
        for path in sorted(root.rglob("*.test.ts*")):
            for invariant in dict.fromkeys(_CITED.findall(path.read_text())):
                where = str(path.relative_to(ROOT))
                if invariant not in registry:
                    unknown.append(f"{where}: {invariant}")
                else:
                    registry[invariant].tests.append(where)
    if unknown:
        raise pytest.UsageError("tests cite undefined invariants:\n  " + "\n  ".join(unknown))
    if config.getoption("check_invariants"):
        untested = [i.id for i in registry.values() if i.status == "enforced" and not i.tests]
        if untested:
            raise pytest.UsageError(f"enforced invariants with no test: {', '.join(untested)}")


def _repro(nodeid: str, text: str) -> str:
    command = f"uv run pytest '{nodeid}'"
    if blob := re.search(r"@reproduce_failure\([^)]*\)", text):
        command += f"  # or add {blob[0]} to the test"
    elif seed := re.search(r"--hypothesis-seed=(\d+)", text):
        command += f" --hypothesis-seed={seed[1]}"
    return command


def pytest_runtest_logreport(report):
    if _config is None:
        return
    if report.when == "call" and report.passed:
        _config._ran.add(report.nodeid)
    if not report.failed or report.when == "teardown":
        return
    config = _config
    registry: dict[str, Invariant] = config._invariants
    declared = [i for i in registry.values() if report.nodeid in i.tests]
    text = str(report.longrepr)
    cited = list(dict.fromkeys(_CITED.findall(text)))
    broken = [i for i in cited if i in registry] or [i.id for i in declared]
    for invariant in broken:
        registry[invariant].failed.append(report.nodeid)
    message = next(
        (line.strip() for line in text.splitlines() if line.lstrip().startswith("E ")), ""
    )
    config._invariant_failures.append(
        {
            "test": report.nodeid,
            "invariants": broken,
            "error": message[2:].strip() if message.startswith("E ") else message,
            "repro": _repro(report.nodeid, text),
            "detail": text[-4000:],
        }
    )


def pytest_terminal_summary(terminalreporter, config):
    registry: dict[str, Invariant] = config._invariants
    exercised = [i for i in registry.values() if i.tests]
    if not exercised:
        return
    terminalreporter.section("invariants")
    for inv in registry.values():
        if not inv.tests:
            state = "untested" if inv.status == "enforced" else "planned"
        elif inv.failed:
            state = "FAILED"
        else:
            ran = [t for t in inv.tests if "::" in t and t in config._ran]
            state = "ok" if ran else "elsewhere"  # only checked by another suite or not run
        terminalreporter.write_line(f"{inv.id:<8} {state:<9} {inv.title}")
    for failure in config._invariant_failures:
        terminalreporter.write_line(
            f"\n{', '.join(failure['invariants']) or '-'}: {failure['error']}\n"
            f"  repro: {failure['repro']}"
        )


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("invariants_report")
    if not path:
        return
    registry: dict[str, Invariant] = session.config._invariants
    report = {
        "ok": exitstatus == 0,
        "invariants": {
            inv.id: {
                "title": inv.title,
                "status": inv.status,
                "tests": len(inv.tests),
                "result": ("failed" if inv.failed else "passed") if inv.tests else "unchecked",
                "failed_tests": inv.failed,
            }
            for inv in registry.values()
        },
        "failures": session.config._invariant_failures,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2) + "\n")
