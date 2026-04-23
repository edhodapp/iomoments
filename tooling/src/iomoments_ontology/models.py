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

from pydantic import BaseModel, ValidationError, model_validator

from iomoments_ontology.types import (
    Cardinality,
    Description,
    ModuleStatus,
    Priority,
    PropertyKind,
    SafeId,
    ShortName,
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

    Phase 1 shape — SysE traceability fields (rationale,
    implementation_refs, verification_refs, status) arrive in Phase 2.
    """

    name: str
    description: str
    entity_ids: list[str] = []
    expression: str = ""


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
    modules: list[ModuleSpec] = []
    data_models: list[DataModel] = []
    external_dependencies: list[ExternalDependency] = []
    open_questions: list[OpenQuestion] = []


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
