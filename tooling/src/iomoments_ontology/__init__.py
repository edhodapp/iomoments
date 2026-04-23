"""iomoments ontology — parallel implementation, forked from python_agent.

Forked 2026-04-23 from python_agent.ontology / dag_utils / types (AGPL
baseline). python_agent has been tabled indefinitely, so iomoments and
fireasmserver (a separate 2026-04-19 fork) evolve in parallel; lessons
that stabilize in either fork are candidates for re-standardization in a
future python_agent successor.

Current state (D009 phase tracking):
- P1 (2026-04-23): baseline pydantic models + DAG persistence.
  HMAC signing and LLM prompt-injection scan dropped per the same
  rationale as fireasmserver (trusted in-repo builder).
- P2 (2026-04-23): SysE traceability fields on DomainConstraint
  (rationale, implementation_refs, verification_refs, status) and
  the PerformanceConstraint type.
- P3 (2026-04-23): content-hash-gated idempotent snapshots, git-SHA
  snapshot labels, fcntl.flock-based dag_transaction for
  concurrent-safe load/modify/save.

Still pending per D009:
- P4: iomoments-specific types (DiagnosticSignal, VerdictNode,
  MomentRepresentation).
- P5: initial iomoments-ontology.json + builder.
- P6: audit-ontology package (requirement → impl → verification
  matrix with gap flagging).
- P7: audit gate wired into pre-push (with its own D-entry).
"""

from iomoments_ontology.dag import (
    dag_transaction,
    git_snapshot_label,
    load_dag,
    make_node_id,
    ontology_content_hash,
    save_dag,
    save_snapshot,
    snapshot_if_changed,
)
from iomoments_ontology.models import (
    ClassSpec,
    DAGEdge,
    DAGNode,
    DataModel,
    Decision,
    DomainConstraint,
    Entity,
    ExternalDependency,
    FunctionSpec,
    ModuleSpec,
    Ontology,
    OntologyDAG,
    OpenQuestion,
    PerformanceConstraint,
    Property,
    PropertyType,
    Relationship,
    validate_ontology_strict,
)
from iomoments_ontology.types import (
    Cardinality,
    Description,
    ModuleStatus,
    PerfDirection,
    Priority,
    PropertyKind,
    RequirementStatus,
    SafeId,
    ShortName,
)

__all__ = [
    "Cardinality",
    "ClassSpec",
    "DAGEdge",
    "DAGNode",
    "DataModel",
    "Decision",
    "Description",
    "DomainConstraint",
    "Entity",
    "ExternalDependency",
    "FunctionSpec",
    "ModuleSpec",
    "ModuleStatus",
    "Ontology",
    "OntologyDAG",
    "OpenQuestion",
    "PerfDirection",
    "PerformanceConstraint",
    "Priority",
    "Property",
    "PropertyKind",
    "PropertyType",
    "Relationship",
    "RequirementStatus",
    "SafeId",
    "ShortName",
    "dag_transaction",
    "git_snapshot_label",
    "load_dag",
    "make_node_id",
    "ontology_content_hash",
    "save_dag",
    "save_snapshot",
    "snapshot_if_changed",
    "validate_ontology_strict",
]
