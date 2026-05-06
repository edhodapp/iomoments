"""D017 tests for the audit's perf-budget pass.

Exercises:
- audit_ontology.perf_budget.check_perf_budget for each direction
  (max/min/equal) at the boundary, just over, and just under.
- Status gating: spec / deviation / n_a are skipped; implemented /
  tested are checked.
- NO_MEASUREMENT mode when no TestResult contains the metric.
- Latest-measurement pick when multiple results for the same env exist.
- audit.run_audit with enforce_perf_budgets on/off.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from audit_ontology import (
    PerfBudgetMode,
    check_perf_budget,
    format_text,
    run_audit,
)
from audit_ontology.cli import main as cli_main
from audit_ontology.perf_budget import _direction_satisfied
from iomoments_ontology import (
    EnvironmentSpec,
    Ontology,
    OntologyDAG,
    PerformanceConstraint,
    TestResult,
    TestResultsDAG,
    TestResultsSnapshot,
    save_snapshot,
    save_test_results_dag,
    save_test_results_snapshot,
)


def _ts(offset_min: int = 0) -> datetime:
    base = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=offset_min)


def _result(
    metric: str, value: float,
    env: EnvironmentSpec | None = None,
    captured_at: datetime | None = None,
    verification_ref: str = "scripts/x.sh",
) -> TestResult:
    return TestResult(
        verification_ref=verification_ref,
        environment=env or EnvironmentSpec(kind="host"),
        outcome="pass",
        captured_git_sha="a" * 40,
        captured_at=captured_at or _ts(),
        measurements={metric: value},
    )


def _row(
    *,
    metric: str = "m",
    budget: float = 100.0,
    direction: str = "max",
    status: str = "implemented",
    unit: str = "ns",
) -> PerformanceConstraint:
    return PerformanceConstraint(
        name="row_" + metric,
        description="d",
        metric=metric,
        budget=budget,
        unit=unit,
        direction=direction,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
    )


# --- status gating ------------------------------------------------------


@pytest.mark.parametrize("status", ["spec", "deviation", "n_a"])
def test_perf_budget_skips_non_implemented_statuses(status: str) -> None:
    """spec / deviation / n_a never trigger budget checks."""
    row = _row(status=status)
    snapshot = TestResultsSnapshot(results=[_result("m", 9999.0)])
    assert not check_perf_budget(row, snapshot)


def test_perf_budget_runs_on_implemented_status() -> None:
    row = _row(status="implemented")
    snapshot = TestResultsSnapshot(results=[_result("m", 9999.0)])
    issues = check_perf_budget(row, snapshot)
    assert len(issues) == 1
    assert issues[0].mode is PerfBudgetMode.BUDGET_VIOLATED


def test_perf_budget_runs_on_tested_status() -> None:
    row = _row(status="tested")
    snapshot = TestResultsSnapshot(results=[_result("m", 9999.0)])
    issues = check_perf_budget(row, snapshot)
    assert len(issues) == 1


# --- direction max ------------------------------------------------------


def test_perf_budget_max_under_budget_no_issue() -> None:
    row = _row(budget=100.0, direction="max")
    snapshot = TestResultsSnapshot(results=[_result("m", 50.0)])
    assert not check_perf_budget(row, snapshot)


def test_perf_budget_max_at_budget_no_issue() -> None:
    """Boundary: equality on max passes (≤ relation)."""
    row = _row(budget=100.0, direction="max")
    snapshot = TestResultsSnapshot(results=[_result("m", 100.0)])
    assert not check_perf_budget(row, snapshot)


def test_perf_budget_max_over_budget_violates() -> None:
    row = _row(budget=100.0, direction="max")
    snapshot = TestResultsSnapshot(results=[_result("m", 100.01)])
    issues = check_perf_budget(row, snapshot)
    assert len(issues) == 1
    assert issues[0].mode is PerfBudgetMode.BUDGET_VIOLATED
    assert issues[0].measured == 100.01
    assert issues[0].budget == 100.0


# --- direction min ------------------------------------------------------


def test_perf_budget_min_above_budget_no_issue() -> None:
    row = _row(budget=100.0, direction="min")
    snapshot = TestResultsSnapshot(results=[_result("m", 200.0)])
    assert not check_perf_budget(row, snapshot)


def test_perf_budget_min_at_budget_no_issue() -> None:
    row = _row(budget=100.0, direction="min")
    snapshot = TestResultsSnapshot(results=[_result("m", 100.0)])
    assert not check_perf_budget(row, snapshot)


def test_perf_budget_min_below_budget_violates() -> None:
    row = _row(budget=100.0, direction="min")
    snapshot = TestResultsSnapshot(results=[_result("m", 99.99)])
    issues = check_perf_budget(row, snapshot)
    assert len(issues) == 1
    assert issues[0].mode is PerfBudgetMode.BUDGET_VIOLATED


# --- direction equal ----------------------------------------------------


def test_perf_budget_equal_at_budget_no_issue() -> None:
    row = _row(budget=42.0, direction="equal")
    snapshot = TestResultsSnapshot(results=[_result("m", 42.0)])
    assert not check_perf_budget(row, snapshot)


def test_perf_budget_equal_off_budget_violates() -> None:
    row = _row(budget=42.0, direction="equal")
    snapshot = TestResultsSnapshot(results=[_result("m", 42.5)])
    issues = check_perf_budget(row, snapshot)
    assert len(issues) == 1


def test_direction_satisfied_unknown_fails_closed() -> None:
    """Defensive: unknown direction returns False (treated as violation).

    PerformanceConstraint.direction is Literal-typed, so this branch
    is unreachable via normal Pydantic-validated rows — but the
    helper itself takes a plain str, so this guards against
    programmer error in future call sites.
    """
    assert _direction_satisfied(50.0, 100.0, "bogus") is False


# --- no measurement -----------------------------------------------------


def test_perf_budget_no_measurement_for_metric() -> None:
    """Snapshot has results but none with the row's metric key."""
    row = _row(metric="m", budget=100.0, direction="max")
    snapshot = TestResultsSnapshot(results=[
        _result("other_metric", 50.0),
    ])
    issues = check_perf_budget(row, snapshot)
    assert len(issues) == 1
    assert issues[0].mode is PerfBudgetMode.NO_MEASUREMENT
    assert issues[0].measured is None


def test_perf_budget_no_measurement_empty_snapshot() -> None:
    row = _row(status="implemented")
    issues = check_perf_budget(row, TestResultsSnapshot())
    assert len(issues) == 1
    assert issues[0].mode is PerfBudgetMode.NO_MEASUREMENT


# --- latest-measurement selection ---------------------------------------


def test_perf_budget_picks_latest_when_multiple_match() -> None:
    """Two results, same env + metric, different captured_at: latest wins.

    Older measurement violates; newer satisfies. The pass must use the
    newer one (snapshot retention is still latest-per-(ref, env), but
    perf-budget keys on metric across producers).
    """
    row = _row(metric="m", budget=100.0, direction="max")
    older = _result(
        "m", 999.0,
        captured_at=_ts(0),
        verification_ref="producer_a",
    )
    newer = _result(
        "m", 50.0,
        captured_at=_ts(60),
        verification_ref="producer_b",
    )
    snapshot = TestResultsSnapshot(results=[older, newer])
    assert not check_perf_budget(row, snapshot)


# --- environment matching ----------------------------------------------


def test_perf_budget_per_env_independent() -> None:
    """Two expected envs, only one has a measurement: one NO_MEASUREMENT."""
    row = PerformanceConstraint(
        name="r", description="d", metric="m",
        budget=100.0, unit="ns", direction="max",
        status="implemented",
        expected_environments=[
            EnvironmentSpec(kind="host"),
            EnvironmentSpec(kind="vmtest", kernel="v5.15"),
        ],
    )
    snapshot = TestResultsSnapshot(results=[
        _result("m", 50.0, env=EnvironmentSpec(kind="host")),
    ])
    issues = check_perf_budget(row, snapshot)
    assert len(issues) == 1
    assert issues[0].mode is PerfBudgetMode.NO_MEASUREMENT
    assert issues[0].environment.kind == "vmtest"


# --- run_audit integration ---------------------------------------------


def _make_dag(
    tmp_path: Path,
    rows: list[PerformanceConstraint],
) -> Path:
    """Build a tiny DAG with the given perf rows; return its path."""
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag, Ontology(performance_constraints=rows), label="seed",
    )
    p = tmp_path / "ontology.json"
    p.write_text(dag.to_json(), encoding="utf-8")
    return p


def _make_results_dag(
    tmp_path: Path, snapshot: TestResultsSnapshot,
) -> Path:
    tr_dag = TestResultsDAG(project_name="iomoments")
    save_test_results_snapshot(tr_dag, snapshot, label="seed")
    p = tmp_path / "test-results.json"
    save_test_results_dag(tr_dag, str(p))
    return p


def test_run_audit_perf_budget_default_off(tmp_path: Path) -> None:
    """Default: enforce_perf_budgets=False → violation does NOT gate."""
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    snapshot = TestResultsSnapshot(results=[_result("m", 9999.0)])
    tr_path = _make_results_dag(tmp_path, snapshot)
    report = run_audit(
        dag_path=dag_path,
        repo_root=tmp_path,
        test_results_dag_path=tr_path,
    )
    assert report.summary.perf_budget_violations == 0
    assert not report.perf_budget_issues


def test_run_audit_perf_budget_flag_on_passing(tmp_path: Path) -> None:
    """Synthetic row + measurement under budget: no perf-budget gap.

    (The synthetic row will still trigger consistency violations
    because it has empty impl/verification refs at status=implemented;
    that's an unrelated check. We assert specifically on the
    perf-budget pass results.)
    """
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    snapshot = TestResultsSnapshot(results=[_result("m", 50.0)])
    tr_path = _make_results_dag(tmp_path, snapshot)
    report = run_audit(
        dag_path=dag_path,
        repo_root=tmp_path,
        test_results_dag_path=tr_path,
        enforce_perf_budgets=True,
    )
    assert report.summary.perf_budget_violations == 0
    assert not report.perf_budget_issues


def test_run_audit_perf_budget_flag_on_violating(tmp_path: Path) -> None:
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    snapshot = TestResultsSnapshot(results=[_result("m", 9999.0)])
    tr_path = _make_results_dag(tmp_path, snapshot)
    report = run_audit(
        dag_path=dag_path,
        repo_root=tmp_path,
        test_results_dag_path=tr_path,
        enforce_perf_budgets=True,
    )
    assert report.summary.perf_budget_violations == 1
    assert report.has_any_gap
    issue = report.perf_budget_issues[0]
    assert issue.mode is PerfBudgetMode.BUDGET_VIOLATED


def test_run_audit_perf_budget_no_measurement_gates(tmp_path: Path) -> None:
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    tr_path = _make_results_dag(tmp_path, TestResultsSnapshot())
    report = run_audit(
        dag_path=dag_path,
        repo_root=tmp_path,
        test_results_dag_path=tr_path,
        enforce_perf_budgets=True,
    )
    assert report.summary.perf_budget_violations == 1
    issue = report.perf_budget_issues[0]
    assert issue.mode is PerfBudgetMode.NO_MEASUREMENT


# --- formatter ---------------------------------------------------------


def test_format_text_renders_perf_budget_section(tmp_path: Path) -> None:
    """A violation should appear under a 'perf budget gaps' header."""
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    snapshot = TestResultsSnapshot(results=[_result("m", 9999.0)])
    tr_path = _make_results_dag(tmp_path, snapshot)
    report = run_audit(
        dag_path=dag_path,
        repo_root=tmp_path,
        test_results_dag_path=tr_path,
        enforce_perf_budgets=True,
    )
    rendered = format_text(report)
    assert "perf budget gaps (D017)" in rendered
    assert "perf budget violated" in rendered
    assert "perf budget gaps      : 1" in rendered


def test_format_text_omits_perf_budget_section_when_clean(
    tmp_path: Path,
) -> None:
    """Clean run: perf-budget header NOT printed; summary count is 0."""
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    snapshot = TestResultsSnapshot(results=[_result("m", 50.0)])
    tr_path = _make_results_dag(tmp_path, snapshot)
    report = run_audit(
        dag_path=dag_path,
        repo_root=tmp_path,
        test_results_dag_path=tr_path,
        enforce_perf_budgets=True,
    )
    rendered = format_text(report)
    assert "perf budget gaps (D017)" not in rendered
    assert "perf budget gaps      : 0" in rendered


def test_format_text_no_unit_when_measured_none(
    tmp_path: Path,
) -> None:
    """NO_MEASUREMENT issue should not append unit after '(none)'."""
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    tr_path = _make_results_dag(tmp_path, TestResultsSnapshot())
    report = run_audit(
        dag_path=dag_path,
        repo_root=tmp_path,
        test_results_dag_path=tr_path,
        enforce_perf_budgets=True,
    )
    rendered = format_text(report)
    assert "measured:         (none)" in rendered
    assert "(none) ns" not in rendered


# --- CLI ---------------------------------------------------------------


def test_cli_default_no_perf_budget_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare invocation: perf-budget pass doesn't run; summary count = 0."""
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    snapshot = TestResultsSnapshot(results=[_result("m", 9999.0)])
    tr_path = _make_results_dag(tmp_path, snapshot)
    rc = cli_main([
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
        "--test-results-dag", str(tr_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "perf budget gaps      : 0" in out


def test_cli_enforce_perf_budgets_violation_gates(
    tmp_path: Path,
) -> None:
    """--enforce-perf-budgets + violation + --exit-nonzero-on-gap → 1."""
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    snapshot = TestResultsSnapshot(results=[_result("m", 9999.0)])
    tr_path = _make_results_dag(tmp_path, snapshot)
    rc = cli_main([
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
        "--test-results-dag", str(tr_path),
        "--enforce-perf-budgets",
        "--exit-nonzero-on-gap",
    ])
    assert rc == 1


def test_cli_enforce_perf_budgets_clean_passes(
    tmp_path: Path,
) -> None:
    """--enforce-perf-budgets + measurement under budget: no perf gap.

    Note: synthetic row has empty refs at status=implemented, which
    surfaces as a consistency violation. Without --exit-nonzero-on-gap,
    rc is 0 regardless.
    """
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    snapshot = TestResultsSnapshot(results=[_result("m", 50.0)])
    tr_path = _make_results_dag(tmp_path, snapshot)
    rc = cli_main([
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
        "--test-results-dag", str(tr_path),
        "--enforce-perf-budgets",
    ])
    assert rc == 0


def test_cli_bootstrap_does_not_apply_to_perf_budgets(
    tmp_path: Path,
) -> None:
    """--bootstrap + --enforce-perf-budgets: bootstrap is freshness-only.

    Per D017: a missing measurement on an implemented row is a real
    gap, not a wiring transient. --bootstrap must not paper over it.
    """
    row = _row(budget=100.0, direction="max")
    dag_path = _make_dag(tmp_path, [row])
    tr_path = _make_results_dag(tmp_path, TestResultsSnapshot())
    rc = cli_main([
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
        "--test-results-dag", str(tr_path),
        "--enforce-perf-budgets",
        "--bootstrap",
        "--exit-nonzero-on-gap",
    ])
    assert rc == 1
