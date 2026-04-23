# pylint: disable=invalid-name  # __main__ is the canonical module name.
"""Module-invocation entry point: ``python -m audit_ontology``."""

from audit_ontology.cli import main

raise SystemExit(main())
