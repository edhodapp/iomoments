"""Shared scalar / literal types for the iomoments ontology schema.

Forked 2026-04-23 from python_agent.types. No divergence from the
baseline in Phase 1 — the ontology-specific extensions in later phases
(DiagnosticSignal verdict kinds, etc.) will land here alongside the
shared primitives.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import StringConstraints

# --- Constrained string types --------------------------------------------

SafeId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-zA-Z0-9_-]+$",
        max_length=100,
    ),
]

ShortName = Annotated[
    str,
    StringConstraints(max_length=100),
]

Description = Annotated[
    str,
    StringConstraints(max_length=2000),
]

# --- Literal enum-like types ---------------------------------------------

PropertyKind = Literal[
    "str",
    "int",
    "float",
    "bool",
    "datetime",
    "entity_ref",
    "list",
    "enum",
]

Cardinality = Literal[
    "one_to_one",
    "one_to_many",
    "many_to_one",
    "many_to_many",
]

ModuleStatus = Literal[
    "not_started",
    "in_progress",
    "complete",
]

Priority = Literal["low", "medium", "high"]
