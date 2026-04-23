"""Ontology and DAG pydantic models for iomoments.

Forked 2026-04-23 from python_agent.ontology. Phase 1 consolidates the
baseline entity / relationship / module / planning-state / DAG model
shapes into a single module. SysE traceability extensions on
DomainConstraint, PerformanceConstraint, iomoments-specific types
(DiagnosticSignal / VerdictNode / MomentRepresentation) land in later
phases — see D009.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from iomoments_ontology.types import (
    Cardinality,
    Description,
    ModuleStatus,
    MomentSpace,
    PerfDirection,
    Priority,
    PropertyKind,
    RequirementStatus,
    SafeId,
    ShortName,
    VerdictKind,
)

# --- Problem domain ------------------------------------------------------


class PropertyType(BaseModel):
    """Type descriptor for an entity property."""

    kind: PropertyKind
    reference: str | list[str] | None = None


class Property(BaseModel):
    """A named, typed property on a domain entity."""

    name: str
    property_type: PropertyType
    description: str = ""
    required: bool = True
    constraints: list[str] = []


class Entity(BaseModel):
    """A business concept in the problem domain."""

    id: SafeId
    name: ShortName
    description: Description = ""
    properties: list[Property] = []


class Relationship(BaseModel):
    """A directed relationship between two entities."""

    source_entity_id: str
    target_entity_id: str
    name: str
    cardinality: Cardinality
    description: str = ""


class DomainConstraint(BaseModel):
    """A domain-level invariant or business rule.

    Phase 2 (D009) added SysE traceability fields so an external
    reviewer can audit any constraint end-to-end without context-
    switching into the codebase:

    - ``rationale``: decision pointer (DECISIONS.md D-entry,
      requirement row ID, or free-text if no formal origin exists).
      Empty string marks an orphan constraint; the audit tool
      (Phase 6) flags those for author attention.
    - ``implementation_refs``: zero-or-more ``file:symbol`` strings
      naming the code that realizes the constraint. Empty list plus
      ``status="spec"`` means "written but not built yet." Refs are
      resolved by the audit tool against the working tree.
    - ``verification_refs``: zero-or-more pointers to a test,
      measurement, or gate proving the constraint holds. Empty list
      alongside ``status="tested"`` or ``"implemented"`` is a
      provable lie — the audit tool flags it.
    - ``status``: requirement lifecycle position. Default ``spec``
      because a newly-authored constraint is, until proven, just a
      written-down intent.
    """

    name: str
    description: str
    entity_ids: list[str] = []
    expression: str = ""
    rationale: str = ""
    implementation_refs: list[str] = []
    verification_refs: list[str] = []
    status: RequirementStatus = "spec"


class PerformanceConstraint(BaseModel):
    """A quantitative performance budget the system must satisfy.

    Distinct from ``DomainConstraint`` because perf rows need the
    budget *number* as first-class data rather than a string in the
    description — the audit tool compares measured values against
    these budgets directly, and the perf-ratchet (future D-entry)
    reads them as its baselines.

    - ``metric``: short identifier emitted by the measurement harness
      (e.g., ``pebay_update_cycles``, ``probe_overhead_ns``,
      ``moments_update_bytes``). Stable across runs; matches the
      harness output key.
    - ``budget``: numeric value the metric is compared against.
    - ``unit``: human-readable unit (``ns``, ``cycles``,
      ``bytes_per_sample``, ``samples_per_sec``). Free-text — the
      measurement harness is authoritative on actual units.
    - ``direction``: comparison direction; see ``PerfDirection``.
    - ``measured_via``: where the measurement comes from (bcc perf
      subsystem, microbenchmark harness, pre-push gate).
    - ``rationale`` / ``implementation_refs`` / ``verification_refs``
      / ``status`` have the same SysE-traceability semantics as on
      ``DomainConstraint``.

    A row with ``status="implemented"`` means we have a budget AND a
    measured value that satisfies ``direction(budget)``. The measured
    value itself is NOT stored here — it lives in whatever perf-
    history artifact the audit tool reads at review time.
    """

    name: str
    description: str
    entity_ids: list[str] = []
    metric: str
    budget: float
    unit: str
    direction: PerfDirection
    measured_via: str = ""
    rationale: str = ""
    implementation_refs: list[str] = []
    verification_refs: list[str] = []
    status: RequirementStatus = "spec"


# --- Solution domain -----------------------------------------------------


class FunctionSpec(BaseModel):
    """Specification for a function to be implemented."""

    name: str
    parameters: list[tuple[str, str]] = []
    return_type: str
    docstring: str = ""
    preconditions: list[str] = []
    postconditions: list[str] = []


class ClassSpec(BaseModel):
    """Specification for a class to be implemented."""

    name: str
    description: str = ""
    bases: list[str] = []
    methods: list[FunctionSpec] = []


class DataModel(BaseModel):
    """Maps a problem-domain entity to a code construct."""

    entity_id: str
    storage: str
    class_name: str
    notes: str = ""


class ExternalDependency(BaseModel):
    """An external package dependency."""

    name: str
    version_constraint: str = ""
    reason: str = ""


class ModuleSpec(BaseModel):
    """Specification for a code module (any language, any layer)."""

    name: str
    responsibility: str
    classes: list[ClassSpec] = []
    functions: list[FunctionSpec] = []
    dependencies: list[str] = []
    test_strategy: str = ""
    status: ModuleStatus = "not_started"


# --- iomoments-specific types (Phase 4, D009) ---------------------------


class DiagnosticSignal(BaseModel):
    """A probe-phase validity indicator feeding the verdict layer.

    Captures the diagnostic battery from D007: Carleman partial sum
    for moment determinacy, Hankel matrix conditioning for atomic
    decomposition, Hill tail-index estimator, KS goodness-of-fit
    against a candidate space (log-normal), half-split moment
    stability for per-moment noise floors. Each signal has a
    measurement method and a set of verdict-entrance thresholds.

    Semantically distinct from PerformanceConstraint: perf rows
    compare measured values to budgets set in advance of any run;
    diagnostic signals are recomputed per run AGAINST the current
    sample and gate whether the moments derived from that sample
    are emissible at all.

    - ``method`` free-text for now (paper reference, formula sketch,
      or algorithm name). Machine-parseable encoding can grow later
      if the audit tool ever wants to cross-check derivations.
    - ``thresholds`` maps a VerdictKind to a free-text entrance
      expression (e.g., ``{"green": "alpha > 2.5", "red":
      "alpha <= 1.0"}``). Strings, not structured predicates —
      D021's draft-first rule applies; structure the thresholds once
      Phase 6 audit actually needs to evaluate them.
    - SysE traceability fields match DomainConstraint/PerformanceConstraint
      so the audit tool treats all three uniformly.
    """

    name: str
    description: str
    method: str = ""
    unit: str = ""
    thresholds: dict[VerdictKind, str] = {}
    rationale: str = ""
    implementation_refs: list[str] = []
    verification_refs: list[str] = []
    status: RequirementStatus = "spec"


class VerdictNode(BaseModel):
    """A verdict category with its entrance criteria and output policy.

    One VerdictNode per value of VerdictKind (green/yellow/amber/red).
    The entrance_criteria list names DiagnosticSignal thresholds that
    must hold for this verdict to fire; the output_policy describes
    what iomoments does when it does.

    Multiple entrance_criteria entries are combined with AND. OR
    between signals is expressed by listing alternative criteria
    strings; the audit tool (Phase 6) will eventually parse them.
    Until then, the strings are documentation.

    Per D007:
      - green: Emit moments with expected error budget.
      - yellow: Emit moments with caveats.
      - amber: Emit moments with a diagnostic recommendation.
      - red: REFUSE moment-based summary; recommend alternative.
    """

    kind: VerdictKind
    description: str
    entrance_criteria: list[str] = []
    output_policy: str = ""
    rationale: str = ""
    implementation_refs: list[str] = []
    verification_refs: list[str] = []
    status: RequirementStatus = "spec"


class MomentRepresentation(BaseModel):
    """A specific (space, order) moment the system can emit.

    D006 emits moments in both raw and log space for each of a small
    set of orders (up through kurtosis at least, higher if the
    diagnostic layer clears it). Enumerating the representations as
    first-class entities gives the ontology something concrete to
    attach DomainConstraints to (e.g., "the 4th log-space moment
    requires diagnostic signal X to be in band Y").

    - ``space``: raw or log (see MomentSpace).
    - ``order``: k ≥ 1. Mean=1, variance=2, skewness=3, kurtosis=4.
    - ``description``: what this representation is useful for.
    - ``notes``: optional free-text; paper refs or cross-checks.

    Lightweight by design — no SysE traceability fields because this
    is a type description, not a requirement. Requirements ABOUT the
    representation live in DomainConstraint entries that reference
    the representation by name.
    """

    space: MomentSpace
    order: int = Field(ge=1)
    description: str = ""
    notes: str = ""


# --- Planning state ------------------------------------------------------


class OpenQuestion(BaseModel):
    """An unresolved design question."""

    id: SafeId
    text: str
    context: str = ""
    priority: Priority = "medium"
    resolved: bool = False
    resolution: str = ""


class Ontology(BaseModel):
    """Complete ontology snapshot."""

    entities: list[Entity] = []
    relationships: list[Relationship] = []
    domain_constraints: list[DomainConstraint] = []
    performance_constraints: list[PerformanceConstraint] = []
    diagnostic_signals: list[DiagnosticSignal] = []
    verdict_nodes: list[VerdictNode] = []
    moment_representations: list[MomentRepresentation] = []
    modules: list[ModuleSpec] = []
    data_models: list[DataModel] = []
    external_dependencies: list[ExternalDependency] = []
    open_questions: list[OpenQuestion] = []

    @model_validator(mode="after")
    def _reject_duplicate_iomoments_rows(self) -> "Ontology":
        """Enforce uniqueness where iomoments types have natural keys.

        - MomentRepresentation: unique on (space, order). Two entries
          with the same (space, order) are silently ambiguous when
          DomainConstraint.description references "the log-3 moment."
        - VerdictNode: at most one per VerdictKind. Contradictory
          output_policy strings on two kind='red' nodes would make
          the verdict semantics undefined.
        """
        seen_reps: set[tuple[str, int]] = set()
        for rep in self.moment_representations:
            key = (rep.space, rep.order)
            if key in seen_reps:
                raise ValueError(
                    f"duplicate MomentRepresentation(space={rep.space}, "
                    f"order={rep.order})"
                )
            seen_reps.add(key)

        seen_kinds: set[str] = set()
        for node in self.verdict_nodes:
            if node.kind in seen_kinds:
                raise ValueError(
                    f"duplicate VerdictNode(kind={node.kind})"
                )
            seen_kinds.add(node.kind)

        return self


# --- DAG structure -------------------------------------------------------


class Decision(BaseModel):
    """Records a design decision carried on a DAG edge.

    The combination of `question` + `options` + `chosen` + `rationale`
    makes every edge auditable: a reader seeing a fork in the DAG can
    reconstruct what alternative designs were under consideration at
    that point in time.
    """

    question: str
    options: list[str]
    chosen: str
    rationale: str


class DAGEdge(BaseModel):
    """An edge in the version DAG.

    Self-loops are rejected at construction time: a node can't be its
    own parent. A full cycle check (detecting loops spanning multiple
    edges) is deferred — it belongs at the DAG level, not the edge
    level, and the P1 builder API doesn't expose a path to create one.
    """

    parent_id: str
    child_id: str
    decision: Decision
    created_at: str

    @model_validator(mode="after")
    def _reject_self_loop(self) -> "DAGEdge":
        if self.parent_id == self.child_id:
            raise ValueError(
                f"DAGEdge self-loop at {self.parent_id}: "
                "parent_id must differ from child_id"
            )
        return self


class DAGNode(BaseModel):
    """A node in the version DAG — a complete ontology snapshot.

    ``integrity_hash`` is retained in the schema for round-trip
    compatibility with python_agent-origin snapshots but is not
    populated by this fork (see iomoments_ontology.__init__ rationale
    on HMAC removal).
    """

    id: str
    ontology: Ontology
    created_at: str
    label: str = ""
    integrity_hash: str = ""


class OntologyDAG(BaseModel):
    """Versioned ontology DAG — the project's requirements history."""

    project_name: str
    nodes: list[DAGNode] = []
    edges: list[DAGEdge] = []
    current_node_id: str = ""

    # --- Navigation ------------------------------------------------------

    def get_node(self, node_id: str) -> DAGNode | None:
        """Find a node by ID."""
        return next(
            (n for n in self.nodes if n.id == node_id), None,
        )

    def get_current_node(self) -> DAGNode | None:
        """Return the currently active node."""
        return self.get_node(self.current_node_id)

    def children_of(self, node_id: str) -> list[DAGNode]:
        """Return all child nodes in edge-insertion order."""
        child_ids = [
            e.child_id for e in self.edges
            if e.parent_id == node_id
        ]
        lookup = {n.id: n for n in self.nodes}
        return [lookup[cid] for cid in child_ids if cid in lookup]

    def parents_of(self, node_id: str) -> list[DAGNode]:
        """Return all parent nodes in edge-insertion order."""
        parent_ids = [
            e.parent_id for e in self.edges
            if e.child_id == node_id
        ]
        lookup = {n.id: n for n in self.nodes}
        return [lookup[pid] for pid in parent_ids if pid in lookup]

    def root_nodes(self) -> list[DAGNode]:
        """Return all nodes with no parents."""
        child_ids = {e.child_id for e in self.edges}
        return [n for n in self.nodes if n.id not in child_ids]

    def edges_from(self, node_id: str) -> list[DAGEdge]:
        """Return all edges from the given node."""
        return [e for e in self.edges if e.parent_id == node_id]

    def edges_to(self, node_id: str) -> list[DAGEdge]:
        """Return all edges to the given node."""
        return [e for e in self.edges if e.child_id == node_id]

    # --- Serialization ---------------------------------------------------

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, text: str) -> "OntologyDAG":
        """Deserialize from JSON string."""
        return cls.model_validate_json(text)


# --- External validation ------------------------------------------------


def _format_validation_error(error: Any) -> str:
    """Render a single pydantic error record as 'loc.loc: message'.

    Typed as Any because pydantic's ErrorDetails TypedDict isn't a
    dict[str, Any] to mypy, and importing ErrorDetails pins us to a
    specific pydantic version's internals.
    """
    loc = ".".join(str(x) for x in error["loc"])
    msg = error["msg"]
    return f"{loc}: {msg}"


def validate_ontology_strict(
    data: Any,
) -> list[str]:
    """Validate ontology data; return list of error strings (empty = ok).

    Accepts Any (not dict[str, Any]) because a raw `json.load` result
    may be a list, int, or None — all of which are reportable input
    errors that should surface as strings here rather than crashing
    with AttributeError inside pydantic internals.
    """
    if not isinstance(data, dict):
        return [f"root: expected an object, got {type(data).__name__}"]
    try:
        Ontology.model_validate(data)
    except ValidationError as exc:
        return [_format_validation_error(e) for e in exc.errors()]
    return []
