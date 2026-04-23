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
        # First char must be alnum or underscore: prevents IDs like
        # "-rf" from being mistaken for CLI flags when passed through
        # shell tooling. Matches fireasmserver's hardened pattern so
        # cross-project audit unification (D009) stays viable.
        pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
        max_length=100,
    ),
]

ShortName = Annotated[
    str,
    StringConstraints(max_length=100),
]

# 4000-char cap matches fireasmserver so snapshots can cross-round-trip.
Description = Annotated[
    str,
    StringConstraints(max_length=4000),
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

# -- SysE traceability lifecycle (Phase 2, D009) --------------------------
#
# Position of a requirement (DomainConstraint or PerformanceConstraint)
# in its lifecycle. Values mirror fireasmserver's RequirementStatus so
# cross-project audit tooling can unify them later.
#
#   spec         - written down but no enforcement code or test yet.
#   tested       - a test exists and passes; enforcement code may be
#                  present but hasn't been cross-verified by mutation.
#   implemented  - enforcement code + test + (where applicable) the
#                  measured value meets the stated budget/invariant.
#   deviation    - the system does NOT satisfy the requirement as
#                  written; the rationale field explains why, and the
#                  audit tool flags this row for human review.
#   n_a          - not applicable to the current build / platform
#                  profile; retained for traceability against the
#                  originating decision.
RequirementStatus = Literal[
    "spec",
    "tested",
    "implemented",
    "deviation",
    "n_a",
]

# Direction of a PerformanceConstraint's budget comparison.
#   max    - measured value MUST be ≤ budget (latency, instruction count,
#            per-sample overhead).
#   min    - measured value MUST be ≥ budget (throughput, samples/sec).
#   equal  - measured value MUST equal budget exactly. Rare; used for
#            protocol constants or fixed-point scale invariants.
PerfDirection = Literal["max", "min", "equal"]

# --- iomoments-specific literals (Phase 4, D009) -------------------------
#
# Verdict categories defined in D007 (core thesis). Each run produces
# one verdict that gates whether the moment-based summary is emitted
# and, if so, with what caveats.
#   green   - moments are a trustworthy shape summary.
#   yellow  - moments are informative but miss some structure
#             (bimodality, etc.). Emitted with caveats.
#   amber   - moments are likely biased (aliasing suspected).
#             Emitted with a diagnostic recommendation.
#   red     - moments are the wrong primitive (heavy tail with
#             non-existent variance). Moment-based summary is REFUSED;
#             iomoments recommends an alternative tool.
VerdictKind = Literal["green", "yellow", "amber", "red"]

# Space in which a moment is expressed. D006 emits both
# representations; downstream consumers select.
#   raw  - moments of the sample value directly.
#   log  - moments of log(sample), converging faster on the
#          log-normal-ish distributions typical of I/O latency.
MomentSpace = Literal["raw", "log"]
