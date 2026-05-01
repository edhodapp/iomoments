"""D015 sub-commit 7f: pytest producer for the test-results DAG.

Buffers passing-test outcomes during the run, writes one snapshot at
session-finish through the existing test_results_dag_transaction
machinery. Failures are NOT emitted (D015 §6: only outcome="pass" is
stored; failures surface via pytest's exit-non-zero, which is its
own kind of artifact).

Architecture:
- ``pytest_runtest_makereport`` collects the (nodeid, outcome) for
  each test's ``call`` phase. Setup/teardown phases are ignored;
  what matters is whether the test body itself passed.
- ``pytest_sessionfinish`` writes the buffered passes to the
  test-results DAG in a single fcntl-locked transaction. One DAG
  snapshot per pytest run, regardless of how many tests passed
  (content-hash dedup makes no-op rebuilds free).

Disable via env var ``IOMOMENTS_TEST_RESULTS_DAG_DISABLE=1`` —
needed when running the plugin's own self-tests (otherwise they'd
recursively try to write to a DAG that's being constructed by the
test fixture).

The producer fills in ``EnvironmentSpec.fix_recipe`` with a pytest
re-run command so audit failures (D015 §5 stale-result mode) print
the exact command needed.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

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


_DISABLE_ENV = "IOMOMENTS_TEST_RESULTS_DAG_DISABLE"


def _producer_disabled() -> bool:
    return os.environ.get(_DISABLE_ENV) == "1"


# Keyed by nodeid, value = True iff the test's call-phase passed.
_passed_nodeids: dict[str, bool] = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None],
) -> Generator[None, Any, None]:
    """Record pass/fail for the call phase of each test."""
    del call  # unused — outcome.get_result() is what we need
    outcome = yield
    if _producer_disabled():
        return
    report = outcome.get_result()
    if report.when != "call":
        return
    # Mark passed iff the call phase had no failure/error AND the
    # test wasn't skipped.
    _passed_nodeids[item.nodeid] = (
        report.outcome == "passed"
    )


def _build_results(
    passed_nodeids: list[str], head_sha: str, captured_at: datetime,
) -> list[TestResult]:
    env = EnvironmentSpec(
        kind="host",
        fix_recipe=".venv/bin/pytest {ref}",
    )
    return [
        TestResult(
            verification_ref=nodeid,
            environment=env,
            outcome="pass",
            captured_git_sha=head_sha,
            captured_at=captured_at,
        )
        for nodeid in passed_nodeids
    ]


def emit_snapshot(
    passed_nodeids: list[str],
    dag_path: Path,
    repo_root: Path,
) -> bool:
    """Write the buffered passes to the test-results DAG at ``dag_path``.

    Returns True iff a snapshot was actually appended (False on
    no-op: no git head, no passed tests, or content unchanged).

    Public (no leading underscore) so the unit tests can call it
    directly with tmp paths instead of running pytest-in-pytest.
    """
    head = git_helpers.head_sha(repo_root)
    if head is None or not passed_nodeids:
        return False

    captured_at = datetime.now(timezone.utc)
    new_results = _build_results(passed_nodeids, head, captured_at)

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
        _, created = snapshot_test_results_if_changed(
            dag, snapshot, label="pytest-session",
        )
        # D015 §4 across-snapshot retention. Every producer write
        # is the right time to prune ancient nodes; cheap when
        # under the threshold (early-return), and bounds DAG file
        # growth without requiring a separate maintenance task.
        prune_test_results_dag_nodes(dag)
    return created


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Persist the run's passes once, at session close."""
    del session, exitstatus  # unused — the buffered passes drive the write
    if _producer_disabled():
        return
    passed = [
        nodeid for nodeid, ok in _passed_nodeids.items() if ok
    ]
    if not passed:
        return
    repo_root = Path(__file__).resolve().parent
    dag_path = (
        repo_root / "tooling" / "iomoments-test-results.json"
    )
    try:
        emit_snapshot(passed, dag_path, repo_root)
    except Exception:  # pylint: disable=broad-except
        # The producer must NEVER cause pytest to exit non-zero
        # because of its own bookkeeping. If persistence fails,
        # log to stderr and move on — the test results themselves
        # are still valid.
        traceback.print_exc()
