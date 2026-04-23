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
            "Exit non-zero if any gap (missing ref or consistency "
            "violation) is found. Default exits 0 regardless."
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
    try:
        report = run_audit(args.dag, args.repo_root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"audit-ontology: tooling error: {exc}", file=sys.stderr)
        return 2
    print(format_text(report))
    if args.exit_nonzero_on_gap and report.has_any_gap:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
