"""D015 sub-commit 7b: persistence tests for TestResultsDAG.

Mirrors the existing iomoments_ontology DAG-persistence tests in
test_audit_ontology.py / test_ontology_phase3.py: load/save round-
trip, content-hash dedup (snapshot_if_changed), atomic write,
fcntl-locked transactions, retention pruning (within-snapshot via
prune_and_add_result, across-snapshot via prune_test_results_dag_nodes).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from iomoments_ontology import (
    DAGEdge,
    Decision,
    EnvironmentSpec,
    TestResult,
    TestResultsDAG,
    TestResultsDAGNode,
    TestResultsSnapshot,
    load_test_results_dag,
    prune_and_add_result,
    prune_test_results_dag_nodes,
    save_test_results_dag,
    save_test_results_snapshot,
    snapshot_test_results_if_changed,
)
# Aliased on import: pytest auto-collects module-level names starting
# with ``test_*`` as test functions; these are persistence helpers, not
# tests. Underscore prefix denotes "test fixture import, not a test".
from iomoments_ontology import (
    test_results_content_hash as _content_hash,
    test_results_dag_transaction as _dag_transaction,
)


def _ts() -> datetime:
    return datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


def _result(
    ref: str = "tests/x.py::test_y",
    env_kind: str = "host",
    env_kernel: str = "",
    sha: str = "a" * 40,
) -> TestResult:
    return TestResult(
        verification_ref=ref,
        environment=EnvironmentSpec(kind=env_kind, kernel=env_kernel),
        outcome="pass",
        captured_git_sha=sha,
        captured_at=_ts(),
    )


# --- load / save round-trip --------------------------------------------


def test_load_missing_file_returns_empty_dag(tmp_path: Path) -> None:
    """File-not-found is the bootstrap case; returns empty DAG."""
    dag = load_test_results_dag(
        str(tmp_path / "absent.json"), project_name="iomoments",
    )
    assert dag.project_name == "iomoments"
    assert dag.nodes == []


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    path = str(tmp_path / "tr.json")
    original = TestResultsDAG(project_name="iomoments")
    save_test_results_snapshot(
        original,
        TestResultsSnapshot(results=[_result()]),
        label="initial",
    )
    save_test_results_dag(original, path)

    restored = load_test_results_dag(path, project_name="iomoments")
    assert restored == original


def test_save_creates_missing_parent_dirs(tmp_path: Path) -> None:
    """save_test_results_dag mkdir -p the parent directory."""
    path = str(tmp_path / "deep" / "nested" / "tr.json")
    save_test_results_dag(
        TestResultsDAG(project_name="iomoments"), path,
    )
    assert Path(path).exists()


def test_save_corrupted_json_raises_on_load(tmp_path: Path) -> None:
    """Corrupted DAG must not be silently replaced — audit reads this."""
    path = tmp_path / "tr.json"
    path.write_text("{not valid json", encoding="utf-8")
    # noqa B017: pydantic raises ValidationError; json raises
    # JSONDecodeError — we don't care which, only that something raises.
    with pytest.raises(Exception):  # noqa: B017
        load_test_results_dag(str(path), project_name="iomoments")


# --- content-hash dedup ------------------------------------------------


def test_content_hash_stable_across_runs() -> None:
    """Same snapshot content → same hash (deterministic)."""
    a = TestResultsSnapshot(results=[_result()])
    b = TestResultsSnapshot(results=[_result()])
    assert _content_hash(a) == _content_hash(b)


def test_content_hash_distinguishes_different_refs() -> None:
    a = TestResultsSnapshot(results=[_result(ref="a")])
    b = TestResultsSnapshot(results=[_result(ref="b")])
    assert _content_hash(a) != _content_hash(b)


def test_snapshot_if_changed_appends_first_time() -> None:
    """Empty DAG bootstrap: always appends regardless of hash."""
    dag = TestResultsDAG(project_name="iomoments")
    snap = TestResultsSnapshot(results=[_result()])
    node_id, created = snapshot_test_results_if_changed(
        dag, snap, label="first",
    )
    assert created is True
    assert dag.current_node_id == node_id
    assert len(dag.nodes) == 1


def test_snapshot_if_changed_dedupes_identical_content() -> None:
    """Identical second snapshot is a no-op (returns existing id)."""
    dag = TestResultsDAG(project_name="iomoments")
    snap = TestResultsSnapshot(results=[_result()])
    first_id, first_created = snapshot_test_results_if_changed(
        dag, snap, label="first",
    )
    second_id, second_created = snapshot_test_results_if_changed(
        dag, snap, label="second",
    )
    assert first_created is True
    assert second_created is False
    assert first_id == second_id
    assert len(dag.nodes) == 1


def test_snapshot_if_changed_appends_when_content_differs() -> None:
    dag = TestResultsDAG(project_name="iomoments")
    snapshot_test_results_if_changed(
        dag, TestResultsSnapshot(results=[_result(ref="a")]), label="a",
    )
    second_id, second_created = snapshot_test_results_if_changed(
        dag,
        TestResultsSnapshot(results=[_result(ref="b")]),
        label="b",
    )
    assert second_created is True
    assert second_id == dag.current_node_id
    assert len(dag.nodes) == 2


# --- prune_and_add_result (within-snapshot retention, D015 §4) ---------


def test_prune_and_add_replaces_existing_ref_env_pair() -> None:
    """A new TestResult for an existing (ref, env) replaces the old."""
    old = _result(ref="t", env_kind="host", sha="a" * 40)
    new = _result(ref="t", env_kind="host", sha="b" * 40)
    snap = TestResultsSnapshot(results=[old])
    out = prune_and_add_result(snap, new)
    assert len(out.results) == 1
    assert out.results[0].captured_git_sha == "b" * 40


def test_prune_and_add_keeps_unrelated_results() -> None:
    """Other (ref, env) pairs are preserved."""
    keep = _result(ref="other", env_kind="host")
    old = _result(ref="t", env_kind="host", sha="a" * 40)
    new = _result(ref="t", env_kind="host", sha="b" * 40)
    snap = TestResultsSnapshot(results=[keep, old])
    out = prune_and_add_result(snap, new)
    refs = {r.verification_ref for r in out.results}
    assert refs == {"other", "t"}
    assert len(out.results) == 2


def test_prune_and_add_distinguishes_environments() -> None:
    """Same ref in different envs are independent records."""
    host = _result(ref="t", env_kind="host", sha="a" * 40)
    vmtest = _result(
        ref="t", env_kind="vmtest", env_kernel="v6.18", sha="a" * 40,
    )
    snap = TestResultsSnapshot(results=[host])
    out = prune_and_add_result(snap, vmtest)
    assert len(out.results) == 2


def test_prune_and_add_into_empty_snapshot() -> None:
    out = prune_and_add_result(
        TestResultsSnapshot(),
        _result(),
    )
    assert len(out.results) == 1


# --- prune_test_results_dag_nodes (across-snapshot retention) ----------


def test_prune_nodes_no_op_under_threshold() -> None:
    """If chain is shorter than keep_last_k, no pruning."""
    dag = TestResultsDAG(project_name="iomoments")
    for i in range(5):
        snapshot_test_results_if_changed(
            dag,
            TestResultsSnapshot(results=[_result(ref=f"r{i}")]),
            label=f"snap-{i}",
        )
    pruned = prune_test_results_dag_nodes(dag, keep_last_k=10)
    assert pruned == 0
    assert len(dag.nodes) == 5


def test_prune_nodes_keeps_last_k_only() -> None:
    """Chain longer than keep_last_k → ancestors past K are pruned."""
    dag = TestResultsDAG(project_name="iomoments")
    for i in range(10):
        snapshot_test_results_if_changed(
            dag,
            TestResultsSnapshot(results=[_result(ref=f"r{i}")]),
            label=f"snap-{i}",
        )
    pruned = prune_test_results_dag_nodes(dag, keep_last_k=3)
    assert pruned == 7
    assert len(dag.nodes) == 3
    # current node still present
    assert dag.get_current_node() is not None


def test_prune_nodes_drops_orphaned_edges() -> None:
    """Edges into pruned nodes must be dropped too."""
    dag = TestResultsDAG(project_name="iomoments")
    for i in range(5):
        snapshot_test_results_if_changed(
            dag,
            TestResultsSnapshot(results=[_result(ref=f"r{i}")]),
            label=f"snap-{i}",
        )
    edges_before = len(dag.edges)
    prune_test_results_dag_nodes(dag, keep_last_k=2)
    # Only the edge connecting the kept pair survives
    assert len(dag.edges) < edges_before
    surviving_node_ids = {n.id for n in dag.nodes}
    for e in dag.edges:
        assert e.parent_id in surviving_node_ids
        assert e.child_id in surviving_node_ids


def test_prune_nodes_preserves_disconnected_branch() -> None:
    """Edges between two nodes neither of which is on the current
    chain must survive pruning. Earlier edge-filter logic dropped
    all edges where either endpoint was outside the kept-on-chain
    set, contradicting the docstring's "disconnected nodes are NOT
    touched" promise. This test pins the corrected behavior.
    """
    # Build a 4-node main chain via the normal API.
    dag = TestResultsDAG(project_name="iomoments")
    for i in range(4):
        snapshot_test_results_if_changed(
            dag,
            TestResultsSnapshot(results=[_result(ref=f"r{i}")]),
            label=f"main-{i}",
        )
    main_chain_node_ids = {n.id for n in dag.nodes}

    # Inject a disconnected pair — two nodes with an edge between
    # them, neither on the main chain.
    iso_a = TestResultsDAGNode(
        id="iso-a",
        snapshot=TestResultsSnapshot(results=[_result(ref="iso_a")]),
        created_at="2026-04-29T11:00:00Z",
        label="iso-a",
    )
    iso_b = TestResultsDAGNode(
        id="iso-b",
        snapshot=TestResultsSnapshot(results=[_result(ref="iso_b")]),
        created_at="2026-04-29T11:01:00Z",
        label="iso-b",
    )
    dag.nodes.extend([iso_a, iso_b])
    dag.edges.append(DAGEdge(
        parent_id="iso-a",
        child_id="iso-b",
        decision=Decision(
            question="iso", options=["x"], chosen="x", rationale="iso",
        ),
        created_at="2026-04-29T11:01:00Z",
    ))

    # Prune main chain to keep_last_k=2 — drops the 2 oldest main
    # chain nodes. The disconnected pair must survive in full.
    prune_test_results_dag_nodes(dag, keep_last_k=2)

    surviving_ids = {n.id for n in dag.nodes}
    assert "iso-a" in surviving_ids, (
        "disconnected node iso-a was dropped — pruning is over-zealous"
    )
    assert "iso-b" in surviving_ids
    iso_edges = [
        e for e in dag.edges
        if e.parent_id == "iso-a" and e.child_id == "iso-b"
    ]
    assert len(iso_edges) == 1, (
        "edge between disconnected nodes was dropped — earlier "
        "edge-filter logic regression"
    )
    # Sanity: 2 main chain nodes survive.
    surviving_main = surviving_ids & main_chain_node_ids
    assert len(surviving_main) == 2


def test_prune_nodes_rejects_zero_or_negative_k() -> None:
    dag = TestResultsDAG(project_name="iomoments")
    with pytest.raises(ValueError, match="keep_last_k must be >= 1"):
        prune_test_results_dag_nodes(dag, keep_last_k=0)
    with pytest.raises(ValueError, match="keep_last_k must be >= 1"):
        prune_test_results_dag_nodes(dag, keep_last_k=-1)


def test_prune_nodes_handles_empty_dag() -> None:
    dag = TestResultsDAG(project_name="iomoments")
    pruned = prune_test_results_dag_nodes(dag, keep_last_k=5)
    assert pruned == 0


# --- transaction context manager ---------------------------------------


def test_transaction_persists_changes_on_normal_exit(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "tr.json")
    with _dag_transaction(path, project_name="iomoments") as dag:
        snapshot_test_results_if_changed(
            dag,
            TestResultsSnapshot(results=[_result()]),
            label="in-transaction",
        )
    # Reload from disk to confirm persistence.
    reloaded = load_test_results_dag(path, project_name="iomoments")
    assert len(reloaded.nodes) == 1


def test_transaction_save_elision_when_unchanged(
    tmp_path: Path,
) -> None:
    """If the DAG isn't mutated inside the transaction, no save fires."""
    path = tmp_path / "tr.json"
    # Seed the file with a known snapshot.
    with _dag_transaction(
        str(path), project_name="iomoments",
    ) as dag:
        snapshot_test_results_if_changed(
            dag,
            TestResultsSnapshot(results=[_result()]),
            label="seed",
        )
    initial_mtime = path.stat().st_mtime_ns

    # Re-enter, do nothing, exit.
    with _dag_transaction(
        str(path), project_name="iomoments",
    ):
        pass

    # mtime must not have changed.
    assert path.stat().st_mtime_ns == initial_mtime


def test_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    """An exception inside the with-block prevents the save."""
    path = str(tmp_path / "tr.json")
    with pytest.raises(RuntimeError, match="boom"):
        with _dag_transaction(
            path, project_name="iomoments",
        ) as dag:
            snapshot_test_results_if_changed(
                dag,
                TestResultsSnapshot(results=[_result()]),
                label="will-be-rolled-back",
            )
            raise RuntimeError("boom")
    # File should not have been written.
    assert not Path(path).exists()


def test_transaction_lock_file_persists(tmp_path: Path) -> None:
    """Lock sidecar persists across transactions (TOCTOU avoidance)."""
    path = str(tmp_path / "tr.json")
    with _dag_transaction(path, project_name="iomoments"):
        pass
    assert Path(path + ".lock").exists()


# --- end-to-end producer pattern ---------------------------------------


def test_end_to_end_producer_pattern(tmp_path: Path) -> None:
    """The full append-a-result-and-persist flow producers will use."""
    path = str(tmp_path / "tr.json")

    # Producer writes one result.
    with _dag_transaction(
        path, project_name="iomoments",
    ) as dag:
        current = dag.get_current_node()
        base = current.snapshot if current else TestResultsSnapshot()
        updated = prune_and_add_result(base, _result(sha="a" * 40))
        snapshot_test_results_if_changed(dag, updated, label="run-1")

    # Same producer writes the same result a second time — should
    # be a no-op (content-hash dedup).
    with _dag_transaction(
        path, project_name="iomoments",
    ) as dag:
        current = dag.get_current_node()
        base = current.snapshot if current else TestResultsSnapshot()
        updated = prune_and_add_result(base, _result(sha="a" * 40))
        snapshot_test_results_if_changed(dag, updated, label="run-2-dup")

    reloaded = load_test_results_dag(path, project_name="iomoments")
    assert len(reloaded.nodes) == 1, (
        "duplicate result should not have created a second snapshot"
    )

    # Producer writes a different result — appends a new snapshot.
    with _dag_transaction(
        path, project_name="iomoments",
    ) as dag:
        current = dag.get_current_node()
        base = current.snapshot if current else TestResultsSnapshot()
        updated = prune_and_add_result(base, _result(sha="b" * 40))
        snapshot_test_results_if_changed(dag, updated, label="run-3-new")

    reloaded = load_test_results_dag(path, project_name="iomoments")
    assert len(reloaded.nodes) == 2

    # Latest snapshot has the newer-sha result.
    latest = reloaded.get_current_node()
    assert latest is not None
    assert len(latest.snapshot.results) == 1
    assert latest.snapshot.results[0].captured_git_sha == "b" * 40


def test_save_test_results_dag_is_atomic_no_partial_file(
    tmp_path: Path,
) -> None:
    """Re-reading a freshly-saved file must yield a complete DAG —
    not a partial JSON. Atomic write via tempfile + os.replace.
    """
    path = str(tmp_path / "tr.json")
    dag = TestResultsDAG(project_name="iomoments")
    save_test_results_snapshot(
        dag, TestResultsSnapshot(results=[_result()]), label="x",
    )
    save_test_results_dag(dag, path)
    # The file must parse cleanly.
    with open(path, encoding="utf-8") as f:
        json.load(f)  # raises if partial
