"""Status ↔ refs consistency checks.

Rules (D009 Phase 2 docstring contracts, enforced here):

* ``status = "spec"``: always OK. New constraint; refs may or may
  not be populated yet.
* ``status = "tested"``: requires at least one verification_ref. A
  tested claim without evidence is a lie (D009 language).
* ``status = "implemented"``: requires implementation_refs AND
  verification_refs. An implementation claim without enforcement
  code OR without tests is incomplete.
* ``status = "deviation"``: requires non-empty rationale. A
  deviation without justification is a silent gap.
* ``status = "n_a"``: always OK. Retained for traceability.

**Scope note.** This checker validates *cardinality* — whether the
right lists are non-empty / the right fields are populated for the
stated status. It does NOT validate *quality* — whether the refs
resolve against the working tree. That second concern lives in
``audit_ontology.resolver`` and rolls up through the row-level
``has_gap`` signal. The two concerns are intentionally separate so
a future maintainer doesn't collapse them: consistency is
ontology-internal (list population), resolution is ontology-vs-tree
(refs point at real code).

The checker returns a list of human-readable violation strings;
empty list means conformant. The caller rolls these up into the
per-constraint section of the audit report.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConstraintFields:
    """The fields the consistency checker cares about.

    Avoids importing the pydantic models directly so the consistency
    layer can be tested with hand-built fixtures, and so the same
    checker runs over DomainConstraint, PerformanceConstraint,
    DiagnosticSignal, and VerdictNode uniformly.
    """

    name: str
    status: str
    rationale: str
    implementation_refs: list[str]
    verification_refs: list[str]


def _check_implemented(fields: ConstraintFields) -> list[str]:
    """Implementation-status sub-rules: both refs lists must be non-empty."""
    violations: list[str] = []
    if not fields.implementation_refs:
        violations.append(
            f"{fields.name}: status='implemented' but "
            "implementation_refs empty"
        )
    if not fields.verification_refs:
        violations.append(
            f"{fields.name}: status='implemented' but "
            "verification_refs empty"
        )
    return violations


def check_status_refs_consistency(
    fields: ConstraintFields,
) -> list[str]:
    """Return a list of consistency violations (empty = OK)."""
    status = fields.status
    if status == "tested" and not fields.verification_refs:
        return [
            f"{fields.name}: status='tested' but verification_refs empty"
        ]
    if status == "implemented":
        return _check_implemented(fields)
    if status == "deviation" and not fields.rationale.strip():
        return [
            f"{fields.name}: status='deviation' but rationale empty"
        ]
    return []
