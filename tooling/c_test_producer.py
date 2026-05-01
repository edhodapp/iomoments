"""D015 sub-commit 7h: producer for the C test suite.

Replaces ``make test-c``'s plain binary-runner loop with a producer
that ALSO emits TestResult records for every test function in every
passing binary.

Convention iomoments' C tests follow (each test file has a main()
that calls each ``test_*()`` function and accumulates failures):
- A binary's exit-0 means EVERY ``test_*()`` function it defines
  passed.
- A binary's exit-non-zero means at least one function failed; we
  cannot tell from outside which one. The producer emits NOTHING
  for that binary — every ref in it becomes ENV_NEVER_EXERCISED
  in the audit (or RUNNER_FORGOT if a prior pass exists). The user
  sees the binary's stderr and re-runs.

Verification-ref shape: matches the ontology's existing pattern,
``tests/c/test_X.c:test_function_name``. The producer parses each
.c file with a regex matching the ``static void test_NAME(void)``
function-definition shape iomoments' tests use uniformly.

Usage::

    python tooling/c_test_producer.py [--build-dir build] \\
        [--test-dir tests/c] [--dag tooling/iomoments-test-results.json]

Returns exit 0 iff every binary passes (preserving ``make test-c``
gating). Exit 1 if any binary fails. The producer's own DAG-
write failures never fail the run (best-effort, like the pytest
producer).
"""

from __future__ import annotations

import argparse
import re
import subprocess
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


_FIX_RECIPE_TEMPLATE = "make test-c"

# Match ``[static] void test_<name>(void)`` at the start of a line.
# iomoments' C tests use this shape uniformly (verified across all
# tests/c/test_*.c). A trailing brace may or may not be on the same
# line; we don't care, only the function name.
_TEST_FN_RE = re.compile(
    r"^\s*(?:static\s+)?void\s+(test_[A-Za-z0-9_]+)\s*\(\s*void\s*\)",
    re.MULTILINE,
)


def _discover_test_functions(c_source: Path) -> list[str]:
    """Return the names of ``test_*`` functions defined in ``c_source``.

    Empty list when the file doesn't exist (caller skips that binary)
    or when the file has no test_* functions (a non-test C file
    happened to live under tests/c/ — unlikely but defensive).
    """
    if not c_source.is_file():
        return []
    text = c_source.read_text(encoding="utf-8")
    return _TEST_FN_RE.findall(text)


def _run_binary(binary: Path) -> tuple[int, str, str]:
    """Run a C test binary, return (returncode, stdout, stderr)."""
    if not binary.is_file():
        return 127, "", f"binary not found: {binary}"
    try:
        result = subprocess.run(
            [str(binary)],
            capture_output=True, text=True,
            check=False, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"binary timed out: {binary}"
    return result.returncode, result.stdout, result.stderr


def _emit_for_pass(
    source: Path,
    test_dir: Path,
    env: EnvironmentSpec,
    head_sha: str,
    captured_at: datetime,
) -> list[TestResult]:
    """Build one TestResult per test_* function defined in source."""
    functions = _discover_test_functions(source)
    if not functions:
        print(
            f"WARN    {source} contains no test_*() functions",
            file=sys.stderr,
        )
    rel = source.relative_to(test_dir.parent.parent)
    return [
        TestResult(
            verification_ref=f"{rel}:{fn}",
            environment=env,
            outcome="pass",
            captured_git_sha=head_sha,
            captured_at=captured_at,
        )
        for fn in functions
    ]


def _build_results(
    test_dir: Path,
    build_dir: Path,
    head_sha: str,
    captured_at: datetime,
) -> tuple[list[TestResult], list[str], list[str]]:
    """Run every test binary, return (passes, failed_binaries, missing)."""
    env = EnvironmentSpec(
        kind="host", fix_recipe=_FIX_RECIPE_TEMPLATE,
    )
    results: list[TestResult] = []
    failed: list[str] = []
    missing: list[str] = []

    for source in sorted(test_dir.glob("test_*.c")):
        binary = build_dir / source.stem
        rc, stdout, stderr = _run_binary(binary)
        if rc == 127:
            missing.append(str(binary))
            print(f"MISSING {binary}", file=sys.stderr)
            continue
        if rc != 0:
            failed.append(str(binary))
            sys.stderr.write(stderr)
            print(f"FAIL    {binary} (exit {rc})", file=sys.stderr)
            continue
        new = _emit_for_pass(
            source, test_dir, env, head_sha, captured_at,
        )
        results.extend(new)
        sys.stdout.write(stdout)
        print(f"PASS    {binary} ({len(new)} test functions)")
    return results, failed, missing


def _emit(
    new_results: list[TestResult], dag_path: Path,
) -> None:
    """Write the buffered passes to the DAG. Best-effort — never raises."""
    if not new_results:
        return
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
            for r in new_results:
                snapshot = prune_and_add_result(snapshot, r)
            snapshot_test_results_if_changed(
                dag, snapshot, label="c-test-suite",
            )
            prune_test_results_dag_nodes(dag)
    except Exception:  # pylint: disable=broad-except
        traceback.print_exc()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit 1 iff any binary failed (preserves make
    test-c gating). DAG persistence is best-effort and never fails
    the run."""
    repo_root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Run C tests, emit per-function TestResults to DAG.",
    )
    parser.add_argument(
        "--build-dir", type=Path,
        default=repo_root_default / "build",
    )
    parser.add_argument(
        "--test-dir", type=Path,
        default=repo_root_default / "tests" / "c",
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

    head = git_helpers.head_sha(args.repo_root)
    captured_at = datetime.now(timezone.utc)

    results, failed, missing = _build_results(
        args.test_dir, args.build_dir,
        head or "0" * 40, captured_at,
    )

    if head is not None and results:
        _emit(results, args.dag)
        print(
            f"c_test_producer: emitted {len(results)} TestResult(s) "
            f"from {len(results)} test functions",
        )

    if failed or missing:
        if failed:
            joined = ", ".join(failed)
            print(
                f"\n{len(failed)} binary failure(s): {joined}",
                file=sys.stderr,
            )
        if missing:
            joined = ", ".join(missing)
            print(
                f"\n{len(missing)} binary missing: {joined}",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
