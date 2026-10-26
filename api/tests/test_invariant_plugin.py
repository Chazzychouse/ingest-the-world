"""The invariant tracker reports which invariant a failure broke, run on one process or spread
over pytest-xdist's workers, where only the workers collect the tests citing each one."""

import json
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1]

CITING = """
import pytest

@pytest.mark.invariant("ACC-6")
def test_breaks_it():
    assert False, "the audit log changed"

@pytest.mark.invariant("ACC-6")
def test_keeps_it():
    pass
"""


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["serial", "xdist"])
def test_a_failure_is_reported_against_the_invariant_its_test_declares(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, workers: list[str]
):
    monkeypatch.setenv("PYTHONPATH", str(API))
    pytester.makepyfile(test_citing=CITING)
    report = pytester.path / "report.json"

    pytester.runpytest_subprocess(
        "-p", "tests.invariants", *workers, f"--invariants-report={report}"
    )

    result = json.loads(report.read_text())
    assert result["invariants"]["ACC-6"]["tests"] == 2
    assert result["invariants"]["ACC-6"]["failed_tests"] == ["test_citing.py::test_breaks_it"]
    [failure] = result["failures"]
    assert failure["invariants"] == ["ACC-6"]
    assert failure["error"].startswith("AssertionError: the audit log changed")
