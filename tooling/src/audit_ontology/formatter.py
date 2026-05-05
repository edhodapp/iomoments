"""Format an AuditReport as human-readable text.

JSON output is deferred — the current consumers (human auditor + CI
gate exit code) don't need it. Grows when Phase 7's CI step wants a
machine-readable diff artifact.
"""

from __future__ import annotations

from audit_ontology.audit import AuditReport, ConstraintReport
from audit_ontology.freshness import FreshnessIssue, FreshnessMode
from audit_ontology.perf_budget import PerfBudgetIssue, PerfBudgetMode
from audit_ontology.resolver import Resolution, ResolvedRef


_STATUS_COL = 14
_NAME_COL = 50
_GAP_COL = 6


def _render_refs(refs: list[ResolvedRef], label: str) -> list[str]:
    """Render a single refs block (implementation OR verification)."""
    if not refs:
        return [f"    {label}: (none)"]
    lines = [f"    {label}:"]
    for r in refs:
        icon = "✓" if r.resolution is Resolution.OK else "✗"
        loc = f" @line {r.line}" if r.line else ""
        notes = f"  — {r.notes}" if r.notes else ""
        lines.append(
            f"      {icon} {r.ref.raw}  [{r.resolution.value}{loc}]{notes}"
        )
    return lines


def _render_row(row: ConstraintReport) -> list[str]:
    marker = "GAP" if row.has_gap else "ok"
    header = (
        f"[{row.kind}] {row.name:<{_NAME_COL}} "
        f"status={row.status:<{_STATUS_COL}} {marker:<{_GAP_COL}}"
    )
    lines = [header]
    if row.consistency_violations:
        lines.append("    consistency:")
        for v in row.consistency_violations:
            lines.append(f"      ✗ {v}")
    lines.extend(_render_refs(row.implementation, "implementation_refs"))
    lines.extend(_render_refs(row.verification, "verification_refs"))
    return lines


_MODE_HEADLINE = {
    FreshnessMode.RUNNER_FORGOT: "runner forgot to fire a test",
    FreshnessMode.STALE_RESULT: "stale result, code edited since last pass",
    FreshnessMode.ENV_NEVER_EXERCISED: "environment never exercised",
    FreshnessMode.UNTRACKED_FILE: "claim references untracked files",
}


_PERF_HEADLINE = {
    PerfBudgetMode.BUDGET_VIOLATED: "perf budget violated",
    PerfBudgetMode.NO_MEASUREMENT: "no measurement for perf budget",
}


def _render_perf_budget_issue(issue: PerfBudgetIssue) -> list[str]:
    """Render a single D017 perf-budget gap."""
    headline = _PERF_HEADLINE[issue.mode]
    env = issue.environment
    env_desc = f"kind={env.kind!r}"
    if env.kernel:
        env_desc += f" kernel={env.kernel!r}"
    if env.distro:
        env_desc += f" distro={env.distro!r}"
    if issue.measured is None:
        measured_line = "   measured:         (none)"
    else:
        measured_line = (
            f"   measured:         {issue.measured:g} {issue.unit}"
        )
    budget_str = f"{issue.direction} {issue.budget:g} {issue.unit}"
    lines = [
        f"×  perf.{issue.claim_name} — {headline}",
        f"   metric:           {issue.metric}",
        f"   budget:           {budget_str}",
        measured_line,
        f"   environment:      ({env_desc})",
        f"   reason:           {issue.reason}",
    ]
    if issue.fix_recipe:
        lines.append(f"   fix:              {issue.fix_recipe}")
    return lines


def _render_freshness_issue(issue: FreshnessIssue) -> list[str]:
    """Render a single freshness gap per D015 §5's three failure modes."""
    headline = _MODE_HEADLINE[issue.mode]
    env = issue.environment
    env_desc = f"kind={env.kind!r}"
    if env.kernel:
        env_desc += f" kernel={env.kernel!r}"
    if env.distro:
        env_desc += f" distro={env.distro!r}"
    lines = [
        f"×  {issue.claim_kind}.{issue.claim_name} — {headline}",
        f"   verification_ref: {issue.verification_ref}",
        f"   environment:      ({env_desc})",
        f"   reason:           {issue.reason}",
    ]
    if issue.fix_recipe:
        lines.append(f"   fix:              {issue.fix_recipe}")
    return lines


def _render_freshness_section(report: AuditReport) -> list[str]:
    if not report.freshness_issues:
        return []
    out = ["-" * 80, "freshness gaps (D015)", "-" * 80]
    for issue in report.freshness_issues:
        out.extend(_render_freshness_issue(issue))
        out.append("")
    return out


def _render_perf_budget_section(report: AuditReport) -> list[str]:
    if not report.perf_budget_issues:
        return []
    out = ["-" * 80, "perf budget gaps (D017)", "-" * 80]
    for issue in report.perf_budget_issues:
        out.extend(_render_perf_budget_issue(issue))
        out.append("")
    return out


def _render_summary(report: AuditReport) -> list[str]:
    s = report.summary
    return [
        "-" * 80,
        "summary",
        "-" * 80,
        f"  total rows            : {s.total_rows}",
        f"  rows with gap         : {s.rows_with_gap}",
        f"  refs resolved         : {s.refs_total}",
        f"  refs file_missing     : {s.refs_file_missing}",
        f"  refs symbol_missing   : {s.refs_symbol_missing}",
        f"  consistency violations: {s.consistency_violations}",
        f"  freshness gaps        : {s.freshness_gaps}",
        f"  perf budget gaps      : {s.perf_budget_violations}",
        "",
    ]


def format_text(report: AuditReport) -> str:
    """Return the full audit report as a single printable string."""
    if not report.rows:
        return "Ontology is empty — nothing to audit.\n"
    out: list[str] = ["=" * 80, "iomoments ontology audit", "=" * 80, ""]
    for row in report.rows:
        out.extend(_render_row(row))
        out.append("")
    out.extend(_render_freshness_section(report))
    out.extend(_render_perf_budget_section(report))
    out.extend(_render_summary(report))
    return "\n".join(out)
