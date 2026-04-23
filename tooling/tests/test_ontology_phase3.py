"""Phase 3 tests (D009): content hashing, git labels, transaction locking.

Covers:
- ontology_content_hash determinism + key-order independence.
- snapshot_if_changed idempotent behavior.
- git_snapshot_label shape with and without git context.
- dag_transaction serialization, save-elision, and rollback-on-exception.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from iomoments_ontology import (
    DomainConstraint,
    Ontology,
    OntologyDAG,
    dag_transaction,
    git_snapshot_label,
    load_dag,
    ontology_content_hash,
    snapshot_if_changed,
)


# --- ontology_content_hash ----------------------------------------------


def test_content_hash_is_stable() -> None:
    """Same-content ontologies hash identically across constructions."""
    ont_a = Ontology(
        domain_constraints=[
            DomainConstraint(name="c1", description="d1"),
        ],
    )
    ont_b = Ontology(
        domain_constraints=[
            DomainConstraint(name="c1", description="d1"),
        ],
    )
    assert ontology_content_hash(ont_a) == ontology_content_hash(ont_b)


def test_content_hash_changes_with_content() -> None:
    """Changing the content changes the hash."""
    ont_a = Ontology()
    ont_b = Ontology(
        domain_constraints=[
            DomainConstraint(name="c1", description="d1"),
        ],
    )
    assert ontology_content_hash(ont_a) != ontology_content_hash(ont_b)


# --- snapshot_if_changed ------------------------------------------------


def test_snapshot_if_changed_appends_once() -> None:
    """Re-running with identical content yields one node, not two."""
    dag = OntologyDAG(project_name="iomoments")
    ont = Ontology(
        domain_constraints=[
            DomainConstraint(name="c", description="d"),
        ],
    )
    first_id, created_first = snapshot_if_changed(dag, ont, "first")
    second_id, created_second = snapshot_if_changed(dag, ont, "second")
    assert created_first is True
    assert created_second is False
    assert first_id == second_id
    assert len(dag.nodes) == 1


def test_snapshot_if_changed_appends_on_real_change() -> None:
    """Changing the ontology content does append a new node."""
    dag = OntologyDAG(project_name="iomoments")
    ont_1 = Ontology()
    ont_2 = Ontology(
        domain_constraints=[
            DomainConstraint(name="c", description="d"),
        ],
    )
    snapshot_if_changed(dag, ont_1, "empty")
    _, created = snapshot_if_changed(dag, ont_2, "populated")
    assert created is True
    assert len(dag.nodes) == 2
    assert len(dag.edges) == 1


# --- git_snapshot_label -------------------------------------------------


def test_git_snapshot_label_has_timestamp() -> None:
    """Label always contains an ISO-UTC timestamp segment."""
    label = git_snapshot_label()
    assert "T" in label and "Z" in label


def test_git_snapshot_label_with_prefix() -> None:
    """Prefix is preserved at the head of the label."""
    label = git_snapshot_label(prefix="phase-3")
    assert label.startswith("phase-3 ")


def test_git_snapshot_label_embeds_sha_when_in_repo() -> None:
    """Inside a git worktree, label embeds '@<short-sha>' (± +dirty)."""
    # The test runs from the iomoments repo checkout, so git is present.
    label = git_snapshot_label()
    assert "@" in label


# --- dag_transaction ----------------------------------------------------


def test_transaction_happy_path_saves(tmp_path: Path) -> None:
    """Normal exit triggers save_dag; next load sees the change."""
    dag_path = str(tmp_path / "dag.json")
    with dag_transaction(dag_path, project_name="iomoments") as dag:
        snapshot_if_changed(dag, Ontology(), "root")
    reloaded = load_dag(dag_path, project_name="iomoments")
    assert len(reloaded.nodes) == 1


def test_transaction_elides_save_when_unchanged(tmp_path: Path) -> None:
    """A transaction that doesn't modify the DAG leaves the mtime stable."""
    dag_path = str(tmp_path / "dag.json")
    with dag_transaction(dag_path, project_name="iomoments") as dag:
        snapshot_if_changed(dag, Ontology(), "root")

    mtime_after_initial = os.stat(dag_path).st_mtime_ns

    # Sleep so a real save would change the mtime even on coarse clocks.
    time.sleep(0.01)
    with dag_transaction(dag_path, project_name="iomoments"):
        pass  # no modification

    mtime_after_noop = os.stat(dag_path).st_mtime_ns
    assert mtime_after_noop == mtime_after_initial


def test_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    """Exception inside yielded block: DAG on disk is unchanged."""
    dag_path = str(tmp_path / "dag.json")
    with dag_transaction(dag_path, project_name="iomoments") as dag:
        snapshot_if_changed(dag, Ontology(), "root")

    marker_hash_before = ontology_content_hash(
        load_dag(dag_path, "iomoments").nodes[0].ontology
    )

    with pytest.raises(RuntimeError, match="boom"):
        with dag_transaction(dag_path, project_name="iomoments") as dag:
            snapshot_if_changed(
                dag,
                Ontology(
                    domain_constraints=[
                        DomainConstraint(
                            name="would_never_land", description="",
                        ),
                    ],
                ),
                "attempted",
            )
            raise RuntimeError("boom")

    reloaded = load_dag(dag_path, project_name="iomoments")
    assert len(reloaded.nodes) == 1  # the second snapshot did NOT persist
    marker_hash_after = ontology_content_hash(reloaded.nodes[0].ontology)
    assert marker_hash_before == marker_hash_after


def test_transaction_creates_missing_parent_dir(tmp_path: Path) -> None:
    """Nested parent directories are created lazily."""
    dag_path = str(tmp_path / "deep" / "nested" / "dag.json")
    with dag_transaction(dag_path, project_name="iomoments") as dag:
        snapshot_if_changed(dag, Ontology(), "root")
    assert os.path.exists(dag_path)
    assert os.path.exists(dag_path + ".lock")


# --- Concurrent safety --------------------------------------------------


def _worker_append(
    worker_args: tuple[str, int],
) -> tuple[str, float, float]:
    """Worker: acquire transaction, append one constraint, save.

    Returns ``(node_id, t_locked, t_released)`` — the wall-clock
    window over which this worker held the transactional lock.

    ``t_locked`` is recorded INSIDE the ``with`` block, after
    ``dag_transaction`` has taken the flock. Recording it before the
    ``with`` would capture "time this worker started trying to
    acquire," which for lock-blocked workers is earlier than the
    previous worker's release and would spuriously report overlap.

    Uses ``time.time()`` rather than ``time.monotonic()`` so values
    are comparable across processes (monotonic's epoch is
    unspecified and process-local).

    Separate top-level fn so multiprocessing's spawn-pickling works.
    """
    dag_path, worker_id = worker_args
    with dag_transaction(dag_path, project_name="iomoments") as dag:
        t_locked = time.time()
        # Hold the lock long enough that any two concurrently-started
        # workers MUST observe serialization. Without this artificial
        # hold, on a fast machine workers can complete in microseconds
        # and the test would silently accept a broken lock.
        time.sleep(0.05)
        cons = DomainConstraint(
            name=f"worker_{worker_id}",
            description=f"from worker {worker_id}",
        )
        current = dag.get_current_node()
        ont = (
            current.ontology.model_copy(deep=True)
            if current is not None
            else Ontology()
        )
        ont.domain_constraints.append(cons)
        node_id, _ = snapshot_if_changed(dag, ont, f"worker-{worker_id}")
    t_released = time.time()
    return node_id, t_locked, t_released


def test_transaction_serializes_concurrent_workers(
    tmp_path: Path,
) -> None:
    """Five processes racing on one DAG file all land their updates
    AND their transactional intervals are disjoint.

    Uses multiprocessing.Pool rather than threads so the advisory
    fcntl.flock actually engages (flock is per-file-description,
    which shared threads in one process don't exercise). Each worker
    sleeps 50ms inside its transaction; if the lock didn't serialize,
    intervals would overlap and the test fails surgically.
    """
    dag_path = str(tmp_path / "dag.json")
    with dag_transaction(dag_path, project_name="iomoments") as dag:
        snapshot_if_changed(dag, Ontology(), "seed")

    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=5) as pool:
        args: list[tuple[str, int]] = [
            (dag_path, i) for i in range(5)
        ]
        results = pool.map(_worker_append, args)

    node_ids = [r[0] for r in results]
    intervals = sorted((r[1], r[2]) for r in results)

    assert len(set(node_ids)) == 5  # no lost updates

    # Intervals sorted by acquire-time: each next acquire must be ≥
    # the prior release. Disjoint intervals = the lock serialized.
    for (_, prev_release), (curr_acquire, _) in zip(
        intervals, intervals[1:],
    ):
        assert curr_acquire >= prev_release, (
            f"transactions overlapped: prev released at "
            f"{prev_release}, next acquired at {curr_acquire}"
        )

    reloaded = load_dag(dag_path, project_name="iomoments")
    # seed + 5 worker snapshots = 6 nodes, 5 edges (linear chain).
    assert len(reloaded.nodes) == 6
    assert len(reloaded.edges) == 5
