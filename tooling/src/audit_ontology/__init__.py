"""audit_ontology — iomoments formal-requirements audit (D009 Phase 6).

Reads the iomoments ontology DAG's current snapshot, cross-references
every DomainConstraint / PerformanceConstraint / DiagnosticSignal /
VerdictNode's ``implementation_refs`` and ``verification_refs`` against
the working tree, runs status ↔ refs consistency checks, and emits a
human-readable matrix with gap flags.

Re-derived from the fireasmserver audit pattern, not copied. The
principles (ref resolution + consistency + matrix output + exit-
nonzero-on-gap for the CI gate) transfer; the files don't.

Phase 6 scope:
- File existence + symbol-grep resolution for Python / C / shell.
- Consistency rules: status='tested' / 'implemented' require refs;
  status='deviation' requires rationale; status='spec' and 'n_a'
  always OK.
- Text-format matrix print + JSON dump.
- CLI with ``--exit-nonzero-on-gap`` for pre-push / CI use
  (Phase 7 wires it).

NOT in Phase 6 scope (would land under D009/D010 later):
- D021's three-level verification chain (traceability + structural
  coverage + mutation verification). This tool implements level 1
  (traceability) at ref-existence granularity. Levels 2 and 3
  require pytest-cov integration and mutation tooling.
"""

from audit_ontology.audit import (
    AuditReport,
    ConstraintReport,
    run_audit,
)
from audit_ontology.consistency import check_status_refs_consistency
from audit_ontology.formatter import format_text
from audit_ontology.parser import ParsedRef, parse_ref
from audit_ontology.resolver import ResolvedRef, Resolution, resolve_ref

__all__ = [
    "AuditReport",
    "ConstraintReport",
    "ParsedRef",
    "Resolution",
    "ResolvedRef",
    "check_status_refs_consistency",
    "format_text",
    "parse_ref",
    "resolve_ref",
    "run_audit",
]
