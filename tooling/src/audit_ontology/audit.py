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


@dataclass
class AuditReport:
    """Full audit output."""

    rows: list[ConstraintReport] = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)
    freshness_issues: list[FreshnessIssue] = field(default_factory=list)

    @property
    def has_any_gap(self) -> bool:
        return (
            self.summary.rows_with_gap > 0
            or self.summary.freshness_gaps > 0
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
) -> AuditReport:
    """Run the audit end-to-end and return an AuditReport.

    Freshness checking (D015 §2) is opt-in via ``enforce_freshness``.
    Default-off so the existing audit gate behavior is preserved
    until producers populate the test-results DAG.

    ``bootstrap`` (D015 §8): when freshness IS enforced, treats
    missing-result issues as warnings (not gaps) — used during the
    producer-wiring window when partial coverage exists. The escape
    valve is removed in a follow-on commit once all producers wire.
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
    for kind, entries in sources:
        for entry in entries:
            row = _build_row(kind, entry, repo_root)
            report.rows.append(row)
            _update_summary(report.summary, row)

    if enforce_freshness:
        _run_freshness_pass(
            report, sources, test_results_dag_path,
            project_name, repo_root, bootstrap,
        )

    return report


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
    test_results_dag_path: Path | None,
    project_name: str,
    repo_root: Path,
    bootstrap: bool,
) -> None:
    """Apply freshness check to every claim and aggregate gaps."""
    snapshot = _load_results_snapshot(
        test_results_dag_path, project_name,
    )
    for kind, entries in sources:
        for entry in entries:
            issues = check_claim_freshness(
                kind, entry, snapshot, repo_root,
            )
            if bootstrap:
                # Bootstrap mode: missing-result and never-exercised
                # downgrade to warnings (recorded but not counted as
                # gaps). STALE_RESULT still counts because that's a
                # genuine freshness regression — the producer fired
                # at one SHA, then code moved past it.
                gap_issues = [
                    i for i in issues
                    if i.mode is FreshnessMode.STALE_RESULT
                ]
            else:
                gap_issues = issues
            report.freshness_issues.extend(issues)
            report.summary.freshness_gaps += len(gap_issues)
