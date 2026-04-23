"""iomoments ontology — parallel implementation, forked from python_agent.

Forked 2026-04-23 from python_agent.ontology / dag_utils / types (AGPL
baseline). python_agent has been tabled indefinitely, so iomoments and
fireasmserver (a separate 2026-04-19 fork) evolve in parallel; lessons
that stabilize in either fork are candidates for re-standardization in a
future python_agent successor.

Phase 1 (this commit): minimal baseline — pydantic model shapes and
DAG persistence. HMAC signing and LLM prompt-injection scan dropped
per the same rationale as fireasmserver (trusted in-repo builder, no
agent-mediated loads).

Upcoming phases (D009 names the scope):
- P2: SysE-grade traceability fields on DomainConstraint +
  PerformanceConstraint type.
- P3: content-hash idempotent snapshot append + git-SHA labels +
  fcntl.flock concurrency.
- P4: iomoments-specific types (DiagnosticSignal, VerdictNode,
  MomentRepresentation).
"""

from iomoments_ontology.dag import (
    load_dag,
    make_node_id,
    save_dag,
    save_snapshot,
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
    Property,
    PropertyType,
    Relationship,
    validate_ontology_strict,
)
from iomoments_ontology.types import (
    Cardinality,
    Description,
    ModuleStatus,
    Priority,
    PropertyKind,
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
    "Priority",
    "Property",
    "PropertyKind",
    "PropertyType",
    "Relationship",
    "SafeId",
    "ShortName",
    "load_dag",
    "make_node_id",
    "save_dag",
    "save_snapshot",
    "validate_ontology_strict",
]
