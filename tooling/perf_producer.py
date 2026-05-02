"""D015 sub-commit 7j: producer for the BPF per-event-overhead
perf measurement (scripts/measure_bpf_overhead.sh).

The perf script writes a small ``perf-summary.txt`` key=value file
alongside its human-readable output:

    per_event_ns_issue=412.50
    per_event_ns_complete=187.30
    events_issue=20015
    events_complete=20015
    kernel=6.17.0-22-generic
    variant=k4

This producer reads that file and emits one TestResult to the
test-results DAG with measurements populated. The audit checks
freshness — was the perf script re-run after src/iomoments.bpf.c
edits? — but does NOT (yet) compare measurements to
PerformanceConstraint budgets; that's a future enrichment.

Usage::

    python tooling/perf_producer.py [--summary build/perf-summary.txt] \\
        [--dag tooling/iomoments-test-results.json]

Invoked automatically by ``make bpf-overhead`` after the perf
script runs. Manual invocation against a stale summary file gives
the producer the captured_git_sha at producer-time, not perf-run-
time, so re-running the perf script is the source-of-truth path.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from audit_ontology import git_helpers
from iomoments_ontology import (
    EnvironmentSpec,
    TestResult,
    TestResultsSnapshot,
    prune_and_add_result,
    prune_test_results_dag_nodes,
    snapshot_test_results_if_changed,
    test_results_dag_transaction,
)


_VERIFICATION_REF = "scripts/measure_bpf_overhead.sh"
_FIX_RECIPE = "sudo make bpf-overhead"

# Keys we extract as measurements (numeric). Other keys (kernel,
# variant) go into env metadata.
_NUMERIC_KEYS = (
    "per_event_ns_issue",
    "per_event_ns_complete",
    "events_issue",
    "events_complete",
)


def _parse_summary(summary_path: Path) -> dict[str, str]:
    """Read key=value lines into a dict; ignore comments / blanks."""
    out: dict[str, str] = {}
    if not summary_path.is_file():
        return out
    text = summary_path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _extract_measurements(summary: dict[str, str]) -> dict[str, float]:
    """Pull out the numeric metrics; skip anything not parseable."""
    out: dict[str, float] = {}
    for key in _NUMERIC_KEYS:
        value = summary.get(key)
        if value is None:
            continue
        try:
            out[key] = float(value)
        except ValueError:
            continue
    return out


def _build_result(
    summary: dict[str, str], head_sha: str, captured_at: datetime,
) -> TestResult | None:
    """Construct a TestResult from the parsed summary, or None on
    no numeric measurements (don't emit an empty-measurements record).
    """
    measurements = _extract_measurements(summary)
    if not measurements:
        return None
    env = EnvironmentSpec(
        kind="host-perf",
        kernel=summary.get("kernel", ""),
        flags={"variant": summary.get("variant", "")},
        fix_recipe=_FIX_RECIPE,
    )
    return TestResult(
        verification_ref=_VERIFICATION_REF,
        environment=env,
        outcome="pass",
        captured_git_sha=head_sha,
        captured_at=captured_at,
        measurements=measurements,
    )


def _persist(new_result: TestResult, dag_path: Path) -> bool:
    """Best-effort DAG write — never raises. Returns True iff a new
    snapshot node was actually appended."""
    try:
        with test_results_dag_transaction(
            str(dag_path), project_name="iomoments",
        ) as dag:
            current = dag.get_current_node()
            snapshot = (
                current.snapshot
                if current is not None
                else TestResultsSnapshot()
            )
            snapshot = prune_and_add_result(snapshot, new_result)
            _, created = snapshot_test_results_if_changed(
                dag, snapshot, label="bpf-overhead-measurement",
            )
            prune_test_results_dag_nodes(dag)
        return created
    except Exception:  # pylint: disable=broad-except
        traceback.print_exc()
        return False


def emit(
    summary_path: Path, dag_path: Path, repo_root: Path,
) -> bool:
    """Emit one TestResult to the DAG. Returns True iff something
    was actually appended (False on no-op: missing summary, no git
    head, malformed summary, content-hash dedup)."""
    if not summary_path.is_file():
        return False
    head = git_helpers.head_sha(repo_root)
    if head is None:
        return False
    summary = _parse_summary(summary_path)
    new_result = _build_result(
        summary, head, datetime.now(timezone.utc),
    )
    if new_result is None:
        return False
    return _persist(new_result, dag_path)


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Always exits 0 — producer never gates the perf run."""
    repo_root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Emit BPF perf-measurement TestResults to the DAG.",
    )
    parser.add_argument(
        "--summary", type=Path,
        default=(
            repo_root_default / "build" / "perf-summary.txt"
        ),
    )
    parser.add_argument(
        "--dag", type=Path,
        default=(
            repo_root_default
            / "tooling"
            / "iomoments-test-results.json"
        ),
    )
    parser.add_argument(
        "--repo-root", type=Path,
        default=repo_root_default,
    )
    args = parser.parse_args(argv)
    created = emit(args.summary, args.dag, args.repo_root)
    status = "emitted 1 TestResult" if created else "no-op"
    print(f"perf_producer: {status} to {args.dag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
