"""Top-level audit: pull the current ontology, resolve every ref,
run the consistency checker, aggregate into an AuditReport.

The inputs are:
- an ontology DAG path (the iomoments-ontology.json file);
- a repo root (absolute path the refs are relative to).

The output is an AuditReport — a flat list of ConstraintReport
records plus a Summary. Callers render the report as text (for
humans) or JSON (for machines) via ``formatter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iomoments_ontology import (
    PerformanceConstraint,
    TestResultsSnapshot,
    load_dag,
    load_test_results_dag,
)

from audit_ontology.consistency import (
    ConstraintFields,
    check_status_refs_consistency,
)
from audit_ontology.freshness import (
    FreshnessIssue,
    FreshnessMode,
    check_claim_freshness,
)
from audit_ontology.parser import parse_ref
from audit_ontology.perf_budget import (
    PerfBudgetIssue,
    check_perf_budget,
)
from audit_ontology.resolver import Resolution, ResolvedRef, resolve_ref


@dataclass
class ConstraintReport:
    """One row in the audit matrix."""

    kind: str                 # "domain" / "perf" / "signal" / "verdict"
    name: str
    status: str
    implementation: list[ResolvedRef]
    verification: list[ResolvedRef]
    consistency_violations: list[str]

    @property
    def has_gap(self) -> bool:
        if self.consistency_violations:
            return True
        for r in (*self.implementation, *self.verification):
            if r.resolution is not Resolution.OK:
                return True
        return False


@dataclass
class Summary:
    """Aggregate numbers for a quick eyeball + CI gate."""

    total_rows: int = 0
    rows_with_gap: int = 0
    refs_total: int = 0
    refs_file_missing: int = 0
    refs_symbol_missing: int = 0
    consistency_violations: int = 0
    freshness_gaps: int = 0
    perf_budget_violations: int = 0


@dataclass
class AuditReport:
    """Full audit output."""

    rows: list[ConstraintReport] = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)
    freshness_issues: list[FreshnessIssue] = field(default_factory=list)
    perf_budget_issues: list[PerfBudgetIssue] = field(default_factory=list)

    @property
    def has_any_gap(self) -> bool:
        return (
            self.summary.rows_with_gap > 0
            or self.summary.freshness_gaps > 0
            or self.summary.perf_budget_violations > 0
        )


def _resolve_list(
    refs: list[str], repo_root: Path,
) -> list[ResolvedRef]:
    out: list[ResolvedRef] = []
    for ref_str in refs:
        if not ref_str.strip():
            continue
        parsed = parse_ref(ref_str)
        out.append(resolve_ref(parsed, repo_root))
    return out


def _build_row(
    kind: str,
    entity: Any,
    repo_root: Path,
) -> ConstraintReport:
    impl = _resolve_list(entity.implementation_refs, repo_root)
    verif = _resolve_list(entity.verification_refs, repo_root)
    violations = check_status_refs_consistency(
        ConstraintFields(
            name=entity.name if hasattr(entity, "name") else str(entity.kind),
            status=entity.status,
            rationale=entity.rationale,
            implementation_refs=entity.implementation_refs,
            verification_refs=entity.verification_refs,
        ),
    )
    return ConstraintReport(
        kind=kind,
        name=entity.name if hasattr(entity, "name") else str(entity.kind),
        status=entity.status,
        implementation=impl,
        verification=verif,
        consistency_violations=violations,
    )


def _update_summary(
    summary: Summary, row: ConstraintReport,
) -> None:
    summary.total_rows += 1
    if row.has_gap:
        summary.rows_with_gap += 1
    summary.consistency_violations += len(row.consistency_violations)
    for r in (*row.implementation, *row.verification):
        summary.refs_total += 1
        if r.resolution is Resolution.FILE_MISSING:
            summary.refs_file_missing += 1
        elif r.resolution is Resolution.SYMBOL_MISSING:
            summary.refs_symbol_missing += 1


def run_audit(
    dag_path: Path,
    repo_root: Path,
    project_name: str = "iomoments",
    test_results_dag_path: Path | None = None,
    enforce_freshness: bool = False,
    bootstrap: bool = False,
    enforce_perf_budgets: bool = False,
) -> AuditReport:
    """Run the audit end-to-end and return an AuditReport.

    Freshness checking (D015 §2) is opt-in via ``enforce_freshness``.
    Default-off so the existing audit gate behavior is preserved
    until producers populate the test-results DAG.

    Perf-budget checking (D017) is opt-in via
    ``enforce_perf_budgets``. Default-off; pre-push wiring flips it
    on once at least one PerformanceConstraint is at status
    ``implemented`` or ``tested``. spec / deviation / n_a rows are
    skipped regardless of the flag.

    ``bootstrap`` (D015 §8): when freshness IS enforced, treats
    missing-result issues as warnings (not gaps) — used during the
    producer-wiring window when partial coverage exists. The escape
    valve is removed in a follow-on commit once all producers wire.
    Bootstrap does NOT apply to perf-budget violations: a row at
    ``implemented`` is opting in to the gate, and a missing
    measurement is a real gap, not a wiring transient.
    """
    dag = load_dag(str(dag_path), project_name=project_name)
    current = dag.get_current_node()
    if current is None:
        return AuditReport()
    ontology = current.ontology

    report = AuditReport()
    sources: list[tuple[str, list[Any]]] = [
        ("domain", list(ontology.domain_constraints)),
        ("perf", list(ontology.performance_constraints)),
        ("signal", list(ontology.diagnostic_signals)),
        ("verdict", list(ontology.verdict_nodes)),
    ]
    _run_static_pass(report, sources, repo_root)
    _run_dynamic_passes(
        report, sources, ontology.performance_constraints,
        test_results_dag_path, project_name, repo_root,
        enforce_freshness, enforce_perf_budgets, bootstrap,
    )
    return report


def _run_static_pass(
    report: AuditReport,
    sources: list[tuple[str, list[Any]]],
    repo_root: Path,
) -> None:
    """Build a row + update summary for every claim, regardless of flags."""
    for kind, entries in sources:
        for entry in entries:
            row = _build_row(kind, entry, repo_root)
            report.rows.append(row)
            _update_summary(report.summary, row)


def _run_dynamic_passes(
    report: AuditReport,
    sources: list[tuple[str, list[Any]]],
    perf_rows: list[PerformanceConstraint],
    test_results_dag_path: Path | None,
    project_name: str,
    repo_root: Path,
    enforce_freshness: bool,
    enforce_perf_budgets: bool,
    bootstrap: bool,
) -> None:
    """Snapshot-dependent passes; no-op if both flags off."""
    if not (enforce_freshness or enforce_perf_budgets):
        return
    snapshot = _load_results_snapshot(test_results_dag_path, project_name)
    if enforce_freshness:
        _run_freshness_pass(report, sources, snapshot, repo_root, bootstrap)
    if enforce_perf_budgets:
        _run_perf_budget_pass(report, perf_rows, snapshot)


def _load_results_snapshot(
    test_results_dag_path: Path | None,
    project_name: str,
) -> TestResultsSnapshot:
    """Load the current test-results snapshot, or empty if absent."""
    if test_results_dag_path is None:
        return TestResultsSnapshot()
    tr_dag = load_test_results_dag(
        str(test_results_dag_path), project_name=project_name,
    )
    current = tr_dag.get_current_node()
    if current is None:
        return TestResultsSnapshot()
    return current.snapshot


def _run_freshness_pass(
    report: AuditReport,
    sources: list[tuple[str, list[Any]]],
    snapshot: TestResultsSnapshot,
    repo_root: Path,
    bootstrap: bool,
) -> None:
    """Apply freshness check to every claim and aggregate gaps."""
    for kind, entries in sources:
        for entry in entries:
            issues = check_claim_freshness(
                kind, entry, snapshot, repo_root,
            )
            if bootstrap:
                # Bootstrap mode: ENV_NEVER_EXERCISED and
                # RUNNER_FORGOT downgrade to warnings (no producer
                # wired yet OR producer didn't fire for this env —
                # both expected during the wiring window).
                #
                # STALE_RESULT continues to gate: a producer fired
                # then code moved past it, a genuine freshness
                # regression unrelated to wiring state.
                #
                # UNTRACKED_FILE continues to gate: it's an upstream
                # "the audit literally cannot apply the rule" not a
                # missing-producer state. Bootstrap mustn't paper
                # over it; the user has to git-add the file.
                gating_modes = {
                    FreshnessMode.STALE_RESULT,
                    FreshnessMode.UNTRACKED_FILE,
                }
                gap_issues = [
                    i for i in issues if i.mode in gating_modes
                ]
            else:
                gap_issues = issues
            report.freshness_issues.extend(issues)
            report.summary.freshness_gaps += len(gap_issues)


def _run_perf_budget_pass(
    report: AuditReport,
    perf_rows: list[PerformanceConstraint],
    snapshot: TestResultsSnapshot,
) -> None:
    """Apply D017 perf-budget check to every PerformanceConstraint.

    Spec / deviation / n_a rows are skipped inside check_perf_budget;
    no bootstrap escape valve (per run_audit's docstring rationale).
    """
    for row in perf_rows:
        issues = check_perf_budget(row, snapshot)
        report.perf_budget_issues.extend(issues)
        report.summary.perf_budget_violations += len(issues)
