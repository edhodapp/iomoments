"""Phase 4 tests (D009): iomoments-specific types.

Covers DiagnosticSignal, VerdictNode, MomentRepresentation and the
Ontology list additions. These types are where iomoments diverges
from fireasmserver's shape; cross-project audit tooling will either
have to treat them as extensions or grow equivalent iomoments-
project-specific handling.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from iomoments_ontology import (
    DiagnosticSignal,
    MomentRepresentation,
    Ontology,
    OntologyDAG,
    VerdictKind,
    VerdictNode,
    save_snapshot,
    validate_ontology_strict,
)


# --- DiagnosticSignal ---------------------------------------------------


def test_diagnostic_signal_minimal() -> None:
    """Only name and description are required; everything else defaults."""
    sig = DiagnosticSignal(
        name="carleman_partial_sum",
        description="Carleman 1926 partial-sum test for moment determinacy.",
    )
    assert sig.method == ""
    assert sig.unit == ""
    assert not sig.thresholds
    assert sig.status == "spec"


def test_diagnostic_signal_fully_populated() -> None:
    """Every field round-trips without loss."""
    sig = DiagnosticSignal(
        name="hill_tail_index",
        description="Hill (1975) tail-index estimator for heavy-tailed I/O.",
        method="alpha_hat = 1 / mean(log(X_i / X_k)) for top-k tail samples.",
        unit="dimensionless",
        thresholds={
            "green": "alpha > 2.5",
            "yellow": "2.0 < alpha <= 2.5",
            "amber": "1.0 < alpha <= 2.0",
            "red": "alpha <= 1.0",
        },
        rationale="D007 thesis: refuse moment emission below alpha=1.",
        implementation_refs=["src/iomoments.c:hill_alpha"],
        verification_refs=["tests/test_hill.py::test_pareto_alpha"],
        status="tested",
    )
    restored = DiagnosticSignal(**sig.model_dump())
    assert restored == sig


def test_diagnostic_signal_threshold_keys_are_verdict_kinds() -> None:
    """Keys of thresholds are constrained to VerdictKind literals."""
    with pytest.raises(ValidationError):
        DiagnosticSignal(
            name="x",
            description="y",
            thresholds={"magenta": "anything"},  # type: ignore[dict-item]
        )


# --- VerdictNode --------------------------------------------------------


def test_verdict_node_minimal() -> None:
    """kind + description are required; criteria/policy default empty."""
    node = VerdictNode(
        kind="green",
        description="Moments are a trustworthy shape summary.",
    )
    assert not node.entrance_criteria
    assert node.output_policy == ""
    assert node.status == "spec"


def test_verdict_node_fully_populated() -> None:
    """Red verdict carries full refusal policy + signal references."""
    node = VerdictNode(
        kind="red",
        description="Moments are the wrong primitive for this workload.",
        entrance_criteria=[
            "hill_tail_index: red",
            "carleman_partial_sum: diverges",
        ],
        output_policy=(
            "Refuse moment-based summary. Recommend DDSketch or HDR "
            "Histogram for the quantile question this workload poses."
        ),
        rationale="D007 core thesis — honest infeasibility reporting.",
        implementation_refs=["src/iomoments.c:emit_red_verdict"],
        verification_refs=["tests/test_verdicts.py::test_red_refuses"],
        status="spec",
    )
    assert node.kind == "red"
    assert len(node.entrance_criteria) == 2
    restored = VerdictNode(**node.model_dump())
    assert restored == node


def test_verdict_node_rejects_invalid_kind() -> None:
    """Only the four verdict kinds are accepted."""
    with pytest.raises(ValidationError):
        VerdictNode(
            kind="chartreuse",  # type: ignore[arg-type]
            description="not a real verdict",
        )


# --- MomentRepresentation -----------------------------------------------


def test_moment_representation_minimal() -> None:
    """space + order are required; description/notes default empty."""
    rep = MomentRepresentation(space="log", order=3)
    assert rep.description == ""
    assert rep.notes == ""


def test_moment_representation_rejects_invalid_space() -> None:
    """Only raw / log are accepted."""
    with pytest.raises(ValidationError):
        MomentRepresentation(
            space="frequency",  # type: ignore[arg-type]
            order=2,
        )


def test_moment_representation_rejects_zero_or_negative_order() -> None:
    """order >= 1 — docstring says k >= 1 and the validator enforces it."""
    with pytest.raises(ValidationError):
        MomentRepresentation(space="raw", order=0)
    with pytest.raises(ValidationError):
        MomentRepresentation(space="log", order=-3)


def test_moment_representation_round_trip() -> None:
    """Full shape survives model_dump round-trip."""
    rep = MomentRepresentation(
        space="log",
        order=4,
        description="Log-space kurtosis — tail-shape indicator for I/O.",
        notes="Pébay 2008 update; D006 emits both raw and log reps.",
    )
    restored = MomentRepresentation(**rep.model_dump())
    assert restored == rep


# --- Ontology integration ----------------------------------------------


def test_ontology_iomoments_lists_default_empty() -> None:
    """All three new lists default to empty on a fresh Ontology."""
    ont = Ontology()
    assert not ont.diagnostic_signals
    assert not ont.verdict_nodes
    assert not ont.moment_representations


def test_ontology_round_trips_iomoments_types() -> None:
    """All three new lists survive JSON round-trip through OntologyDAG."""
    ont = Ontology(
        diagnostic_signals=[
            DiagnosticSignal(
                name="ks_lognormal_pvalue",
                description=(
                    "Kolmogorov-Smirnov goodness-of-fit against "
                    "log-normal for space-selection."
                ),
                unit="probability",
                thresholds={"green": "p > 0.05"},
            ),
        ],
        verdict_nodes=[
            VerdictNode(
                kind="amber",
                description="Moments likely biased (aliasing suspected).",
                entrance_criteria=["half_split_stability: amber"],
                output_policy=(
                    "Emit moments with a diagnostic recommendation "
                    "to increase sample window."
                ),
            ),
        ],
        moment_representations=[
            MomentRepresentation(space="raw", order=1),
            MomentRepresentation(space="log", order=3),
        ],
    )
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(dag, ont, label="phase-4")
    restored = OntologyDAG.from_json(dag.to_json())
    ront = restored.nodes[0].ontology
    assert ront.diagnostic_signals[0].name == "ks_lognormal_pvalue"
    assert ront.verdict_nodes[0].kind == "amber"
    assert {r.space for r in ront.moment_representations} == {"raw", "log"}
    assert [r.order for r in ront.moment_representations] == [1, 3]


def test_validate_ontology_strict_rejects_bad_iomoments_data() -> None:
    """Strict validator surfaces malformed iomoments types."""
    bad = {
        "verdict_nodes": [
            {
                "kind": "blue",
                "description": "y",
            }
        ]
    }
    errors = validate_ontology_strict(bad)
    assert errors
    assert any("kind" in e for e in errors)


def test_validate_ontology_strict_rejects_bad_diagnostic_threshold_key() -> (
    None
):
    """A threshold dict with a non-VerdictKind key is surfaced via Ontology."""
    bad = {
        "diagnostic_signals": [
            {
                "name": "sig",
                "description": "y",
                "thresholds": {"cerulean": "anything"},
            }
        ]
    }
    errors = validate_ontology_strict(bad)
    assert errors
    assert any("thresholds" in e for e in errors)


def test_validate_ontology_strict_rejects_bad_moment_space() -> None:
    """A MomentRepresentation with a bogus space is surfaced."""
    bad = {
        "moment_representations": [
            {"space": "frequency", "order": 2}
        ]
    }
    errors = validate_ontology_strict(bad)
    assert errors
    assert any("space" in e for e in errors)


def test_ontology_rejects_duplicate_moment_representation() -> None:
    """Two MomentRepresentation(space, order) pairs can't coexist."""
    with pytest.raises(
        ValidationError, match="duplicate MomentRepresentation",
    ):
        Ontology(
            moment_representations=[
                MomentRepresentation(space="raw", order=1),
                MomentRepresentation(space="raw", order=1),
            ],
        )


def test_ontology_rejects_duplicate_verdict_kind() -> None:
    """Two VerdictNodes with the same kind can't coexist."""
    with pytest.raises(ValidationError, match="duplicate VerdictNode"):
        Ontology(
            verdict_nodes=[
                VerdictNode(
                    kind="red",
                    description="first",
                    output_policy="refuse",
                ),
                VerdictNode(
                    kind="red",
                    description="second",
                    output_policy="also refuse (contradiction!)",
                ),
            ],
        )


def test_ontology_accepts_all_four_distinct_verdicts() -> None:
    """One of each kind coexist happily."""
    kinds: list[VerdictKind] = ["green", "yellow", "amber", "red"]
    ont = Ontology(
        verdict_nodes=[
            VerdictNode(kind=k, description=k)
            for k in kinds
        ],
    )
    assert len(ont.verdict_nodes) == 4
