"""audit-ontology CLI.

Usage::

    audit-ontology
    audit-ontology --exit-nonzero-on-gap
    audit-ontology --dag path/to/ontology.json --repo-root path/to/repo

The bare invocation always exits 0 (so manual inspection of the
matrix is friction-free); callers that want a gate use
``--exit-nonzero-on-gap`` to turn any detected gap into a nonzero
exit code (Phase 7 wires pre-push / CI with this flag).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from audit_ontology.audit import run_audit
from audit_ontology.formatter import format_text

# __file__ = <repo>/tooling/src/audit_ontology/cli.py
# parents[0]=audit_ontology, [1]=src, [2]=tooling, [3]=<repo-root>.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DAG = _REPO_ROOT / "tooling" / "iomoments-ontology.json"
_DEFAULT_TEST_RESULTS_DAG = (
    _REPO_ROOT / "tooling" / "iomoments-test-results.json"
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the iomoments ontology against the working tree.",
    )
    parser.add_argument(
        "--dag",
        type=Path,
        default=_DEFAULT_DAG,
        help=f"Ontology DAG JSON path (default: {_DEFAULT_DAG}).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help=f"Repo root refs resolve against (default: {_REPO_ROOT}).",
    )
    parser.add_argument(
        "--exit-nonzero-on-gap",
        action="store_true",
        help=(
            "Exit non-zero if any gap (missing ref, consistency "
            "violation, freshness gap if --enforce-freshness, or "
            "perf-budget gap if --enforce-perf-budgets) is found. "
            "Default exits 0 regardless."
        ),
    )
    parser.add_argument(
        "--enforce-freshness",
        action="store_true",
        help=(
            "Enable D015 freshness checking. Default off — only "
            "ref-resolution and consistency are checked. Once "
            "producers are wired (D015 §7), pre-push enables this."
        ),
    )
    parser.add_argument(
        "--enforce-perf-budgets",
        action="store_true",
        help=(
            "Enable D017 perf-budget checking. Default off — "
            "PerformanceConstraint rows at status=implemented/tested "
            "are checked against the latest TestResult.measurements; "
            "violations and missing measurements are reported as gaps."
        ),
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "D015 §8 escape valve: when --enforce-freshness is set, "
            "downgrade missing-result and never-exercised gaps to "
            "warnings. Used during the producer-wiring window. Stale "
            "results (real freshness regressions) still gate. Does "
            "NOT apply to --enforce-perf-budgets (per D017)."
        ),
    )
    parser.add_argument(
        "--test-results-dag",
        type=Path,
        default=_DEFAULT_TEST_RESULTS_DAG,
        help=(
            "Test-results DAG JSON path (default: "
            f"{_DEFAULT_TEST_RESULTS_DAG}). Consulted when "
            "--enforce-freshness or --enforce-perf-budgets is set."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Exit-code taxonomy (standard Unix convention):

    * 0 — audit completed; no gaps (or gaps present but
      ``--exit-nonzero-on-gap`` not set).
    * 1 — audit completed and found gaps
      (only when ``--exit-nonzero-on-gap`` is set).
    * 2 — tooling error (bad DAG path, malformed JSON, etc.).
      Always raised regardless of ``--exit-nonzero-on-gap`` so a
      CI gate can distinguish "your code has gaps" from
      "the audit tool broke."

    Called directly and via the ``audit-ontology`` console-script
    entry in pyproject.toml.
    """
    argv_list = sys.argv[1:] if argv is None else list(argv)
    args = _parse_args(argv_list)
    needs_results_dag = (
        args.enforce_freshness or args.enforce_perf_budgets
    )
    try:
        report = run_audit(
            args.dag, args.repo_root,
            test_results_dag_path=(
                args.test_results_dag if needs_results_dag else None
            ),
            enforce_freshness=args.enforce_freshness,
            bootstrap=args.bootstrap,
            enforce_perf_budgets=args.enforce_perf_budgets,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"audit-ontology: tooling error: {exc}", file=sys.stderr)
        return 2
    print(format_text(report))
    if args.exit_nonzero_on_gap and report.has_any_gap:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
