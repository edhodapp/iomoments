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

from iomoments_ontology import load_dag

from audit_ontology.consistency import (
    ConstraintFields,
    check_status_refs_consistency,
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


@dataclass
class AuditReport:
    """Full audit output."""

    rows: list[ConstraintReport] = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)

    @property
    def has_any_gap(self) -> bool:
        return self.summary.rows_with_gap > 0


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
) -> AuditReport:
    """Run the audit end-to-end and return an AuditReport."""
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
    return report
