"""Schema tests for D015's TestResult / EnvironmentSpec / TestResultsDAG.

Exercises the natural-key dedup logic, JSON round-trip, and the
TestResultsSnapshot validator that rejects duplicate
(verification_ref, environment) pairs. Persistence and audit-
freshness behavior land in subsequent D015 sub-commits and have
their own test files.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from iomoments_ontology import (
    EnvironmentSpec,
    TestResult,
    TestResultsDAG,
    TestResultsDAGNode,
    TestResultsSnapshot,
)


def _ts() -> datetime:
    """A fixed UTC timestamp for deterministic tests."""
    return datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


def _result(
    ref: str = "tests/x.py::test_y",
    env_kind: str = "host",
    sha: str = "a" * 40,
    measurements: dict[str, float] | None = None,
) -> TestResult:
    return TestResult(
        verification_ref=ref,
        environment=EnvironmentSpec(kind=env_kind),
        outcome="pass",
        captured_git_sha=sha,
        captured_at=_ts(),
        measurements=measurements or {},
    )


# --- EnvironmentSpec ---------------------------------------------------


def test_environment_defaults() -> None:
    """All fields except kind are optional with sensible defaults."""
    env = EnvironmentSpec(kind="host")
    assert env.kernel == ""
    assert env.distro == ""
    assert env.arch == "x86_64"
    assert not env.flags
    assert env.fix_recipe == ""


def test_environment_natural_key_excludes_fix_recipe() -> None:
    """fix_recipe is metadata, not part of identity — two envs with
    different recipes but identical kind/kernel/distro/arch/flags
    must collide on natural_key."""
    a = EnvironmentSpec(kind="vmtest", kernel="v6.18", fix_recipe="cmd-A")
    b = EnvironmentSpec(kind="vmtest", kernel="v6.18", fix_recipe="cmd-B")
    assert a.natural_key() == b.natural_key()


def test_environment_natural_key_distinguishes_kernel() -> None:
    """Same kind, different kernel → different natural_key."""
    a = EnvironmentSpec(kind="vmtest", kernel="v5.15")
    b = EnvironmentSpec(kind="vmtest", kernel="v6.18")
    assert a.natural_key() != b.natural_key()


def test_environment_natural_key_flags_are_sorted_canonically() -> None:
    """Same flags in different insertion order → same natural_key."""
    a = EnvironmentSpec(kind="host", flags={"x": "1", "y": "2"})
    b = EnvironmentSpec(kind="host", flags={"y": "2", "x": "1"})
    assert a.natural_key() == b.natural_key()


# --- TestResult --------------------------------------------------------


def test_testresult_requires_pass_outcome() -> None:
    """Per D015 §6, only outcome='pass' is stored."""
    with pytest.raises(ValidationError):
        TestResult(
            verification_ref="x",
            environment=EnvironmentSpec(kind="host"),
            outcome="fail",  # type: ignore[arg-type]
            captured_git_sha="a" * 40,
            captured_at=_ts(),
        )


def test_testresult_measurements_default_empty() -> None:
    r = _result()
    assert not r.measurements


def test_testresult_measurements_populated() -> None:
    r = _result(measurements={"per_event_ns": 412.5})
    assert r.measurements["per_event_ns"] == pytest.approx(412.5)


def test_testresult_json_round_trip() -> None:
    """Serialize → deserialize must reconstruct an equal object."""
    original = _result(
        ref="tests/c/test_pebay.c:test_tiny_stream",
        env_kind="vmtest",
        measurements={"cycles_per_sample": 17.3},
    )
    text = original.model_dump_json()
    restored = TestResult.model_validate_json(text)
    assert restored == original


# --- TestResultsSnapshot duplicate-rejection ---------------------------


def test_snapshot_accepts_unique_results() -> None:
    """Two TestResults with different verification_refs in the same
    environment are NOT duplicates."""
    snap = TestResultsSnapshot(results=[
        _result(ref="tests/a.py::test_x"),
        _result(ref="tests/a.py::test_y"),
    ])
    assert len(snap.results) == 2


def test_snapshot_accepts_same_ref_in_different_envs() -> None:
    """Same verification_ref across two distinct environments is
    valid — that's how cross-env coverage works."""
    snap = TestResultsSnapshot(results=[
        TestResult(
            verification_ref="t",
            environment=EnvironmentSpec(kind="host"),
            outcome="pass",
            captured_git_sha="a" * 40,
            captured_at=_ts(),
        ),
        TestResult(
            verification_ref="t",
            environment=EnvironmentSpec(
                kind="vmtest", kernel="v5.15",
            ),
            outcome="pass",
            captured_git_sha="a" * 40,
            captured_at=_ts(),
        ),
    ])
    assert len(snap.results) == 2


def test_snapshot_rejects_same_ref_in_same_env() -> None:
    """Two TestResults sharing (verification_ref, env.natural_key())
    must be rejected — within-snapshot retention is latest-passing-
    per-(ref, env). Persistence layer handles pruning."""
    with pytest.raises(ValidationError, match="duplicate TestResult"):
        TestResultsSnapshot(results=[
            _result(ref="t", env_kind="host", sha="a" * 40),
            _result(ref="t", env_kind="host", sha="b" * 40),
        ])


def test_snapshot_rejects_collision_when_fix_recipe_differs() -> None:
    """fix_recipe is not part of natural_key — two results with the
    same (ref, kind/kernel/distro/arch/flags) collide regardless of
    fix_recipe content."""
    base_kwargs = {
        "verification_ref": "t",
        "outcome": "pass",
        "captured_git_sha": "a" * 40,
        "captured_at": _ts(),
    }
    with pytest.raises(ValidationError, match="duplicate TestResult"):
        TestResultsSnapshot(results=[
            TestResult(
                environment=EnvironmentSpec(
                    kind="vmtest", kernel="v6.18",
                    fix_recipe="cmd-A",
                ),
                **base_kwargs,  # type: ignore[arg-type]
            ),
            TestResult(
                environment=EnvironmentSpec(
                    kind="vmtest", kernel="v6.18",
                    fix_recipe="cmd-B",
                ),
                **base_kwargs,  # type: ignore[arg-type]
            ),
        ])


# --- TestResultsDAG ----------------------------------------------------


def test_dag_empty_construction() -> None:
    dag = TestResultsDAG(project_name="iomoments")
    assert dag.project_name == "iomoments"
    assert not dag.nodes
    assert not dag.edges
    assert dag.current_node_id == ""
    assert dag.get_current_node() is None


def test_dag_get_node_returns_match_or_none() -> None:
    dag = TestResultsDAG(
        project_name="iomoments",
        nodes=[TestResultsDAGNode(
            id="n1",
            snapshot=TestResultsSnapshot(),
            created_at="2026-04-29T12:00:00Z",
        )],
    )
    assert dag.get_node("n1") is not None
    assert dag.get_node("nonexistent") is None


def test_dag_root_nodes_finds_unparented() -> None:
    """A node with no inbound edges is a root."""
    dag = TestResultsDAG(
        project_name="iomoments",
        nodes=[
            TestResultsDAGNode(
                id="root",
                snapshot=TestResultsSnapshot(),
                created_at="2026-04-29T12:00:00Z",
            ),
            TestResultsDAGNode(
                id="child",
                snapshot=TestResultsSnapshot(),
                created_at="2026-04-29T12:01:00Z",
            ),
        ],
    )
    # Without edges, both are roots.
    roots = dag.root_nodes()
    assert {n.id for n in roots} == {"root", "child"}


def test_dag_rejects_duplicate_node_ids() -> None:
    """A duplicate node ID makes get_node ambiguous; pin the validator."""
    with pytest.raises(ValidationError, match="duplicate TestResultsDAGNode"):
        TestResultsDAG(
            project_name="iomoments",
            nodes=[
                TestResultsDAGNode(
                    id="dup",
                    snapshot=TestResultsSnapshot(),
                    created_at="2026-04-29T12:00:00Z",
                ),
                TestResultsDAGNode(
                    id="dup",
                    snapshot=TestResultsSnapshot(),
                    created_at="2026-04-29T12:01:00Z",
                ),
            ],
        )


def test_dag_json_round_trip() -> None:
    original = TestResultsDAG(
        project_name="iomoments",
        nodes=[TestResultsDAGNode(
            id="n1",
            snapshot=TestResultsSnapshot(results=[_result()]),
            created_at="2026-04-29T12:00:00Z",
            label="initial",
        )],
        current_node_id="n1",
    )
    restored = TestResultsDAG.from_json(original.to_json())
    assert restored == original
    assert restored.get_current_node() is not None
    assert restored.get_current_node().id == "n1"  # type: ignore[union-attr]
