"""Pytest plugin tying tests to the invariants in docs/spec/03-invariants.md.

- `@pytest.mark.invariant("ACC-1", ...)` declares which invariants a test enforces.
  Citing an ID the doc doesn't define is a collection error.
- `check(id, condition, message)` fails with the ID in the message,
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
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
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


def check(invariant: str, condition: object, message: str) -> None:
    if not condition:
        raise InvariantViolation(invariant, message)


@dataclass(frozen=True, slots=True)
class Failure:
    test: str
    invariants: list[str]
    error: str
    repro: str
    detail: str


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


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("invariants")
    group.addoption(
        "--check-invariants",
        action="store_true",
        help="fail if an enforced invariant has no test",
    )
    group.addoption("--invariants-report", metavar="PATH", help="write a JSON report")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "invariant(*ids): invariants this test enforces")
    config.pluginmanager.register(InvariantPlugin(config, load()), "invariant-tracker")


def _external_citations() -> Iterator[tuple[str, str]]:
    """(location, invariant) for each `[ID]` cited in tests outside pytest."""
    for root in EXTERNAL_TESTS:
        for path in sorted(root.rglob("*.test.ts*")):
            for invariant in dict.fromkeys(_CITED.findall(path.read_text())):
                yield str(path.relative_to(ROOT)), invariant


def _repro(nodeid: str, text: str) -> str:
    command = f"uv run pytest '{nodeid}'"
    if blob := re.search(r"@reproduce_failure\([^)]*\)", text):
        return f"{command}  # or add {blob[0]} to the test"
    if seed := re.search(r"--hypothesis-seed=(\d+)", text):
        return f"{command} --hypothesis-seed={seed[1]}"
    return command


def _first_error_line(text: str) -> str:
    for line in text.splitlines():
        if line.lstrip().startswith("E "):
            return line.strip()[2:].strip()
    return ""


class InvariantPlugin:
    """Tracks which tests cite which invariants, and what happened to them."""

    def __init__(self, config: pytest.Config, registry: dict[str, Invariant]):
        self.config = config
        self.registry = registry
        self.failures: list[Failure] = []
        self.passed: set[str] = set()

    def _cite(self, where: str, invariant: str, unknown: list[str]) -> None:
        if invariant in self.registry:
            self.registry[invariant].tests.append(where)
        else:
            unknown.append(f"{where}: {invariant}")

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        unknown: list[str] = []
        for item in items:
            for marker in item.iter_markers("invariant"):
                for invariant in marker.args:
                    self._cite(item.nodeid, invariant, unknown)
        for where, invariant in _external_citations():
            self._cite(where, invariant, unknown)
        if unknown:
            raise pytest.UsageError("tests cite undefined invariants:\n  " + "\n  ".join(unknown))
        if self.config.getoption("check_invariants"):
            untested = [
                i.id for i in self.registry.values() if i.status == "enforced" and not i.tests
            ]
            if untested:
                raise pytest.UsageError(f"enforced invariants with no test: {', '.join(untested)}")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)
        if not report.failed or report.when == "teardown":
            return
        text = str(report.longrepr)
        cited = [i for i in dict.fromkeys(_CITED.findall(text)) if i in self.registry]
        declared = [i.id for i in self.registry.values() if report.nodeid in i.tests]
        broken = cited or declared
        for invariant in broken:
            self.registry[invariant].failed.append(report.nodeid)
        self.failures.append(
            Failure(
                test=report.nodeid,
                invariants=broken,
                error=_first_error_line(text),
                repro=_repro(report.nodeid, text),
                detail=text[-4000:],
            )
        )

    def _state(self, inv: Invariant) -> str:
        if not inv.tests:
            return "untested" if inv.status == "enforced" else "planned"
        if inv.failed:
            return "FAILED"
        # Tests outside pytest (no "::"), or not selected in this run, are checked elsewhere.
        return "ok" if any(t in self.passed for t in inv.tests) else "elsewhere"

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        if not any(i.tests for i in self.registry.values()):
            return
        terminalreporter.section("invariants")
        for inv in self.registry.values():
            terminalreporter.write_line(f"{inv.id:<8} {self._state(inv):<9} {inv.title}")
        for failure in self.failures:
            ids = ", ".join(failure.invariants) or "-"
            terminalreporter.write_line(f"\n{ids}: {failure.error}\n  repro: {failure.repro}")

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        path = self.config.getoption("invariants_report")
        if not path:
            return
        report = {
            "ok": exitstatus == 0,
            "invariants": {
                inv.id: {
                    "title": inv.title,
                    "status": inv.status,
                    "tests": len(inv.tests),
                    "result": "unchecked"
                    if not inv.tests
                    else ("failed" if inv.failed else "passed"),
                    "failed_tests": inv.failed,
                }
                for inv in self.registry.values()
            },
            "failures": [asdict(f) for f in self.failures],
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(report, indent=2) + "\n")
