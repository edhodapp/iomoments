"""Format an AuditReport as human-readable text.

JSON output is deferred — the current consumers (human auditor + CI
gate exit code) don't need it. Grows when Phase 7's CI step wants a
machine-readable diff artifact.
"""

from __future__ import annotations

from audit_ontology.audit import AuditReport, ConstraintReport
from audit_ontology.freshness import FreshnessIssue, FreshnessMode
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


def format_text(report: AuditReport) -> str:
    """Return the full audit report as a single printable string."""
    if not report.rows:
        return "Ontology is empty — nothing to audit.\n"

    out: list[str] = []
    out.append("=" * 80)
    out.append("iomoments ontology audit")
    out.append("=" * 80)
    out.append("")

    for row in report.rows:
        out.extend(_render_row(row))
        out.append("")

    if report.freshness_issues:
        out.append("-" * 80)
        out.append("freshness gaps (D015)")
        out.append("-" * 80)
        for issue in report.freshness_issues:
            out.extend(_render_freshness_issue(issue))
            out.append("")

    summary = report.summary
    out.append("-" * 80)
    out.append("summary")
    out.append("-" * 80)
    out.append(f"  total rows            : {summary.total_rows}")
    out.append(f"  rows with gap         : {summary.rows_with_gap}")
    out.append(f"  refs resolved         : {summary.refs_total}")
    out.append(f"  refs file_missing     : {summary.refs_file_missing}")
    out.append(f"  refs symbol_missing   : {summary.refs_symbol_missing}")
    out.append(
        f"  consistency violations: {summary.consistency_violations}",
    )
    out.append(f"  freshness gaps        : {summary.freshness_gaps}")
    out.append("")
    return "\n".join(out)
