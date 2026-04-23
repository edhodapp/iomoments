"""Phase 2 tests for SysE extensions (D009).

Covers:
- DomainConstraint's new traceability fields (rationale,
  implementation_refs, verification_refs, status) with defaults
  and non-default values.
- PerformanceConstraint shape with all fields exercised.
- RequirementStatus / PerfDirection literal validation.
- Ontology.performance_constraints list round-trip.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from iomoments_ontology import (
    DomainConstraint,
    Ontology,
    OntologyDAG,
    PerformanceConstraint,
    save_snapshot,
    validate_ontology_strict,
)


def test_domain_constraint_defaults() -> None:
    """A minimal DomainConstraint gets the documented defaults."""
    cons = DomainConstraint(
        name="pebay_update_is_numerically_stable",
        description="Order-k moment updates resist cancellation.",
    )
    assert cons.rationale == ""
    assert not cons.implementation_refs
    assert not cons.verification_refs
    assert cons.status == "spec"


def test_domain_constraint_with_syse_fields() -> None:
    """All SysE fields accept the shapes documented in D009."""
    cons = DomainConstraint(
        name="moments_are_finite",
        description="Every reported moment must be a finite float.",
        rationale="D007: verdicts refuse to emit when math is invalid.",
        implementation_refs=[
            "src/iomoments.bpf.c:pebay_update",
            "src/iomoments.c:aggregate_moments",
        ],
        verification_refs=[
            "tests/test_pebay_ref.py:test_moments_are_finite",
        ],
        status="implemented",
    )
    assert cons.status == "implemented"
    assert len(cons.implementation_refs) == 2
    assert cons.verification_refs[0].startswith("tests/")


def test_requirement_status_rejects_bogus_value() -> None:
    """Literal types catch typos at construction time."""
    with pytest.raises(ValidationError):
        DomainConstraint(
            name="x",
            description="y",
            status="shipped",  # type: ignore[arg-type]
        )


def test_performance_constraint_minimal() -> None:
    """PerformanceConstraint requires metric/budget/unit/direction."""
    perf = PerformanceConstraint(
        name="pebay_update_cycles_per_sample",
        description="Per-sample Pébay update cost ceiling.",
        metric="pebay_update_cycles",
        budget=200.0,
        unit="cycles",
        direction="max",
    )
    assert perf.status == "spec"
    assert perf.measured_via == ""
    assert perf.rationale == ""


def test_performance_constraint_full() -> None:
    """All fields of a fully-populated PerformanceConstraint round-trip."""
    perf = PerformanceConstraint(
        name="probe_phase_overhead",
        description="Probe phase must not dominate the hot path.",
        entity_ids=["probe"],
        metric="probe_overhead_ns",
        budget=500.0,
        unit="ns",
        direction="max",
        measured_via="bcc perf-event microbench",
        rationale="D007 diagnostic layer must be cheap enough to stay on.",
        implementation_refs=["src/iomoments.c:run_probe"],
        verification_refs=["tests/perf/test_probe_overhead.py"],
        status="tested",
    )
    data = perf.model_dump()
    restored = PerformanceConstraint(**data)
    assert restored == perf


def test_perf_direction_rejects_bogus_value() -> None:
    """Only max/min/equal are accepted for direction."""
    with pytest.raises(ValidationError):
        PerformanceConstraint(
            name="x",
            description="y",
            metric="m",
            budget=1.0,
            unit="u",
            direction="ascending",  # type: ignore[arg-type]
        )


def test_ontology_performance_constraints_round_trip() -> None:
    """performance_constraints is a first-class list on Ontology."""
    perf = PerformanceConstraint(
        name="moments_update_bytes",
        description="Per-CPU update must fit in one cache line.",
        metric="moments_update_bytes",
        budget=64.0,
        unit="bytes",
        direction="max",
    )
    ont = Ontology(performance_constraints=[perf])
    restored = Ontology.model_validate(ont.model_dump())
    assert len(restored.performance_constraints) == 1
    assert restored.performance_constraints[0].metric == "moments_update_bytes"


def test_syse_fields_survive_dag_snapshot() -> None:
    """Full traceability fields round-trip through save_snapshot + JSON."""
    cons = DomainConstraint(
        name="verdict_red_refuses_emission",
        description=(
            "When the diagnostic battery returns Red, iomoments must "
            "not emit moment-based summary statistics."
        ),
        rationale="D007 core thesis.",
        implementation_refs=["src/iomoments.c:emit_verdict"],
        verification_refs=["tests/test_verdicts.py:test_red_refuses"],
        status="spec",
    )
    ont = Ontology(domain_constraints=[cons])
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(dag, ont, label="phase-2-smoke")
    restored = OntologyDAG.from_json(dag.to_json())
    rcons = restored.nodes[0].ontology.domain_constraints[0]
    assert rcons.rationale == "D007 core thesis."
    assert rcons.status == "spec"
    assert rcons.implementation_refs == ["src/iomoments.c:emit_verdict"]
    assert rcons.verification_refs == [
        "tests/test_verdicts.py:test_red_refuses"
    ]


def test_performance_constraints_defaults_empty() -> None:
    """Ontology.performance_constraints defaults to an empty list."""
    ont = Ontology()
    assert not ont.performance_constraints


def test_status_accepts_deviation_and_na() -> None:
    """deviation and n_a are load-bearing lifecycle positions."""
    deviant = DomainConstraint(
        name="legacy_workload_exempted",
        description="Legacy fixture known not to satisfy D.",
        status="deviation",
    )
    not_applicable = DomainConstraint(
        name="arm64_only",
        description="Applies on aarch64 only.",
        status="n_a",
    )
    assert deviant.status == "deviation"
    assert not_applicable.status == "n_a"


def test_implemented_with_empty_refs_is_constructible() -> None:
    """Pins the CURRENT (Phase 2) behavior: no constructor-time check.

    D009's docstring calls out that status='implemented' alongside
    empty implementation_refs is a 'provable lie'. The Phase 6 audit
    tool flags it; the pydantic model does NOT reject it at
    construction time because authors create constraints iteratively
    (set status, then fill refs in a follow-up edit). This test exists
    so if Phase 6 ever tightens the construction-time contract, the
    author has to update this test and think about in-flight states.
    """
    cons = DomainConstraint(
        name="orphan_pending_refs",
        description="Placeholder for a real constraint.",
        status="implemented",
    )
    assert not cons.implementation_refs
    assert cons.status == "implemented"


def test_validate_ontology_strict_rejects_bad_performance_constraint() -> None:
    """validate_ontology_strict surfaces malformed perf rows clearly."""
    bad = {
        "performance_constraints": [
            {
                "name": "x",
                "description": "y",
                "metric": "m",
                "budget": "not a number",
                "unit": "ns",
                "direction": "max",
            }
        ]
    }
    errors = validate_ontology_strict(bad)
    assert errors
    assert any("budget" in e for e in errors)
