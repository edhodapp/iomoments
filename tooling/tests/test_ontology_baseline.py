"""Phase 1 round-trip and navigation tests for iomoments_ontology.

Verifies the baseline fork behaves identically to python_agent's
ontology DAG for the un-extended shapes. Phase 2/3/4 features get
their own test modules.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from iomoments_ontology import (
    DAGEdge,
    DAGNode,
    Decision,
    DomainConstraint,
    Entity,
    ModuleSpec,
    Ontology,
    OntologyDAG,
    Property,
    PropertyType,
    Relationship,
    load_dag,
    save_dag,
    save_snapshot,
    validate_ontology_strict,
)


def _sample_ontology() -> Ontology:
    """Build a tiny but non-trivial ontology for round-trip tests."""
    workload = Entity(
        id="workload",
        name="Workload",
        description="An observed I/O latency stream.",
        properties=[
            Property(
                name="source_pid",
                property_type=PropertyType(kind="int"),
                description="Origin process PID.",
            ),
        ],
    )
    moment = Entity(
        id="moment",
        name="Moment",
        description="An order-k moment summary.",
    )
    rel = Relationship(
        source_entity_id="workload",
        target_entity_id="moment",
        name="summarized_by",
        cardinality="one_to_many",
    )
    cons = DomainConstraint(
        name="moments_are_finite",
        description="Every reported moment must be finite.",
        entity_ids=["moment"],
    )
    mod = ModuleSpec(
        name="tests.test_pebay_ref",
        responsibility="Pébay numerical oracle.",
        status="in_progress",
    )
    return Ontology(
        entities=[workload, moment],
        relationships=[rel],
        domain_constraints=[cons],
        modules=[mod],
    )


def test_empty_dag_round_trips() -> None:
    """An empty DAG serializes and deserializes without loss."""
    dag = OntologyDAG(project_name="iomoments")
    text = dag.to_json()
    restored = OntologyDAG.from_json(text)
    assert restored == dag


def test_populated_dag_round_trips() -> None:
    """A DAG with nodes + edges survives JSON serialization."""
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(dag, _sample_ontology(), label="initial")
    save_snapshot(
        dag,
        _sample_ontology(),
        label="follow-up",
        decision=Decision(
            question="Extend entity set?",
            options=["defer", "add Verdict"],
            chosen="defer",
            rationale="Phase 4 scope, not Phase 1.",
        ),
    )
    restored = OntologyDAG.from_json(dag.to_json())
    assert restored.project_name == "iomoments"
    assert len(restored.nodes) == 2
    assert len(restored.edges) == 1
    assert restored.current_node_id == dag.current_node_id


def test_navigation_methods() -> None:
    """children_of / parents_of / root_nodes / edges_from, edges_to."""
    dag = OntologyDAG(project_name="iomoments")
    root_id = save_snapshot(dag, Ontology(), label="root")
    mid_id = save_snapshot(dag, Ontology(), label="mid")
    leaf_id = save_snapshot(dag, Ontology(), label="leaf")

    assert dag.get_node(root_id) is not None
    assert dag.get_node("missing") is None
    current = dag.get_current_node()
    assert current is not None and current.id == leaf_id

    assert [n.id for n in dag.children_of(root_id)] == [mid_id]
    assert [n.id for n in dag.parents_of(leaf_id)] == [mid_id]
    assert [n.id for n in dag.root_nodes()] == [root_id]

    out_edges = dag.edges_from(root_id)
    in_edges = dag.edges_to(leaf_id)
    assert len(out_edges) == 1 and out_edges[0].child_id == mid_id
    assert len(in_edges) == 1 and in_edges[0].parent_id == mid_id


def test_parent_edge_on_first_save() -> None:
    """First save has no parent edge; subsequent saves chain off current."""
    dag = OntologyDAG(project_name="iomoments")
    first_id = save_snapshot(dag, Ontology(), label="first")
    assert not dag.edges
    assert dag.current_node_id == first_id

    second_id = save_snapshot(dag, Ontology(), label="second")
    assert len(dag.edges) == 1
    assert dag.edges[0].parent_id == first_id
    assert dag.edges[0].child_id == second_id


def test_load_dag_returns_empty_on_missing(tmp_path: Path) -> None:
    """load_dag on a non-existent file returns a fresh empty DAG."""
    dag = load_dag(
        str(tmp_path / "does-not-exist.json"),
        project_name="iomoments",
    )
    assert dag.project_name == "iomoments"
    assert dag.nodes == []
    assert dag.edges == []


def test_load_dag_raises_on_corrupt_file(tmp_path: Path) -> None:
    """Validation failure is a hard error — we don't silently replace."""
    corrupt = tmp_path / "dag.json"
    corrupt.write_text("{not-json")
    with pytest.raises(ValidationError):
        load_dag(str(corrupt), project_name="iomoments")


def test_save_and_load_round_trip_on_disk(tmp_path: Path) -> None:
    """save_dag -> load_dag preserves structure exactly."""
    out = tmp_path / "nested" / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(dag, _sample_ontology(), label="initial")
    save_dag(dag, str(out))
    restored = load_dag(str(out), project_name="iomoments")
    assert restored.model_dump() == dag.model_dump()


def test_validate_ontology_strict_ok() -> None:
    """Valid data yields an empty error list."""
    data = _sample_ontology().model_dump()
    assert validate_ontology_strict(data) == []


def test_validate_ontology_strict_rejects() -> None:
    """Invalid data yields a non-empty error list, not an exception."""
    bad = {"entities": [{"id": "x", "name": 123}]}
    errors = validate_ontology_strict(bad)
    assert errors  # non-empty
    assert any("name" in e for e in errors)


def test_validate_ontology_strict_non_dict() -> None:
    """Non-dict input is reported, not raised from pydantic internals."""
    bad_inputs: list[Any] = [[], "string", 42, None]
    for bad in bad_inputs:
        errors = validate_ontology_strict(bad)
        assert errors
        assert errors[0].startswith("root:")


def test_dagedge_and_dagnode_direct_construction() -> None:
    """The lower-level types build cleanly without the helpers."""
    node = DAGNode(
        id="n1",
        ontology=Ontology(),
        created_at="2026-04-23T00:00:00+00:00",
        label="manual",
    )
    edge = DAGEdge(
        parent_id="n0",
        child_id="n1",
        decision=Decision(
            question="q",
            options=["a"],
            chosen="a",
            rationale="r",
        ),
        created_at="2026-04-23T00:00:00+00:00",
    )
    assert node.id == "n1"
    assert edge.child_id == "n1"


def test_dagedge_rejects_self_loop() -> None:
    """A node can't be its own parent — the validator fires."""
    with pytest.raises(ValidationError, match="self-loop"):
        DAGEdge(
            parent_id="n1",
            child_id="n1",
            decision=Decision(
                question="q",
                options=["a"],
                chosen="a",
                rationale="r",
            ),
            created_at="2026-04-23T00:00:00+00:00",
        )


def test_navigation_on_empty_dag() -> None:
    """Every navigation accessor returns empty on an empty DAG."""
    dag = OntologyDAG(project_name="iomoments")
    assert dag.get_node("anything") is None
    assert dag.get_current_node() is None
    assert dag.children_of("anything") == []
    assert dag.parents_of("anything") == []
    assert dag.root_nodes() == []
    assert dag.edges_from("anything") == []
    assert dag.edges_to("anything") == []


def test_navigation_on_diamond_pattern() -> None:
    """A diamond (one root, two middles, one leaf) reports correctly.

    Layout:        root
                  /    \\
               mid_a  mid_b
                  \\    /
                   leaf
    """
    now = datetime.now(timezone.utc).isoformat()

    def _decision(rationale: str) -> Decision:
        return Decision(
            question="q",
            options=["a"],
            chosen="a",
            rationale=rationale,
        )

    nodes = [
        DAGNode(id=nid, ontology=Ontology(), created_at=now, label=nid)
        for nid in ("root", "mid_a", "mid_b", "leaf")
    ]
    edges = [
        DAGEdge(
            parent_id="root",
            child_id="mid_a",
            decision=_decision("left-branch"),
            created_at=now,
        ),
        DAGEdge(
            parent_id="root",
            child_id="mid_b",
            decision=_decision("right-branch"),
            created_at=now,
        ),
        DAGEdge(
            parent_id="mid_a",
            child_id="leaf",
            decision=_decision("rejoin-left"),
            created_at=now,
        ),
        DAGEdge(
            parent_id="mid_b",
            child_id="leaf",
            decision=_decision("rejoin-right"),
            created_at=now,
        ),
    ]
    dag = OntologyDAG(project_name="iomoments", nodes=nodes, edges=edges)

    assert [n.id for n in dag.root_nodes()] == ["root"]
    # children_of preserves edge-insertion order: mid_a was added first.
    assert [n.id for n in dag.children_of("root")] == ["mid_a", "mid_b"]
    # leaf has two parents — order reflects edge insertion.
    assert [n.id for n in dag.parents_of("leaf")] == ["mid_a", "mid_b"]


def test_save_dag_cleans_up_tempfile_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing os.replace leaves no .tmp file behind and re-raises."""
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(dag, Ontology(), label="initial")
    target = tmp_path / "dag.json"

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated rename failure"):
        save_dag(dag, str(target))

    # No tempfiles left behind, and the target was never created.
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert not target.exists()
