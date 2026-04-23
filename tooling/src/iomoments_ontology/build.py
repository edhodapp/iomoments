"""Build the iomoments ontology DAG from the YAML authoring surface.

Flow:
  YAML source  ->  pydantic validation  ->  dag_transaction  ->  JSON DAG

Invoked manually today; wired into the pre-push audit gate in Phase 7.
Idempotent: re-running with unchanged content is a no-op thanks to
snapshot_if_changed. Concurrent-safe across processes via
dag_transaction's fcntl.flock.

Usage::

    python -m iomoments_ontology.build
    python -m iomoments_ontology.build --source other.yaml --dag other.json
    build-iomoments-ontology    # console-script alias
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from iomoments_ontology import (
    Decision,
    Ontology,
    dag_transaction,
    git_snapshot_label,
    ontology_content_hash,
    snapshot_if_changed,
)

# __file__ = <repo>/tooling/src/iomoments_ontology/build.py
# parents[0]=iomoments_ontology, [1]=src, [2]=tooling, [3]=<repo-root>.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SOURCE = _REPO_ROOT / "tooling" / "iomoments-ontology.yaml"
_DEFAULT_DAG = _REPO_ROOT / "tooling" / "iomoments-ontology.json"
_PROJECT_NAME = "iomoments"


def _load_yaml_source(path: Path) -> dict[str, Any]:
    """Read the YAML source and return its top-level dict.

    Rejects non-dict top-levels (list, scalar, None) at this layer so
    the pydantic validator downstream gets a shape it expects.
    """
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: YAML root must be a mapping, got "
            f"{type(data).__name__}",
        )
    return data


def build_ontology_from_yaml(source_path: Path) -> Ontology:
    """Load + validate the YAML source into an Ontology.

    Pydantic does all the work: literal checks, natural-key uniqueness
    on MomentRepresentation / VerdictNode, SysE field types, etc. A
    ValidationError here points at a schema mismatch in the YAML.

    Public: the audit tool and other consumers can call this
    directly when they need the Ontology object without the DAG
    snapshot / git labels / lock-file ceremony of ``build()``.
    """
    raw = _load_yaml_source(source_path)
    return Ontology.model_validate(raw)


def _build_decision(source_path: Path, content_hash: str) -> Decision:
    """Synthesize a Decision record for the DAG edge to the new node.

    The canonical Decision record on a scripted-builder edge is
    thin — most of the interesting design decisions live inside the
    ontology itself. This captures the builder invocation and the
    content hash so an auditor can correlate an edge to a specific
    source-file state.
    """
    return Decision(
        question=f"Rebuild {_PROJECT_NAME} ontology from {source_path.name}?",
        options=["regenerate", "skip"],
        chosen="regenerate",
        rationale=f"source={source_path.name}; content_hash={content_hash}",
    )


def build(
    source_path: Path = _DEFAULT_SOURCE,
    dag_path: Path = _DEFAULT_DAG,
    label_prefix: str = "builder",
) -> tuple[str, bool]:
    """Run the build. Returns (node_id, created).

    `created` is True when a new snapshot was appended, False when
    the content hash matched the current DAG node and the append
    was elided.
    """
    ontology = build_ontology_from_yaml(source_path)
    content_hash = ontology_content_hash(ontology)
    label = git_snapshot_label(prefix=label_prefix)
    decision = _build_decision(source_path, content_hash)

    with dag_transaction(str(dag_path), _PROJECT_NAME) as dag:
        node_id, created = snapshot_if_changed(
            dag, ontology, label, decision,
        )
    return node_id, created


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the iomoments ontology DAG from YAML.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=_DEFAULT_SOURCE,
        help=f"YAML source path (default: {_DEFAULT_SOURCE}).",
    )
    parser.add_argument(
        "--dag",
        type=Path,
        default=_DEFAULT_DAG,
        help=f"DAG JSON path (default: {_DEFAULT_DAG}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Called both directly and via the ``build-iomoments-ontology``
    console-script entry in pyproject.toml. setuptools wraps the
    return value with sys.exit, so returning 0 yields exit-code 0.
    """
    argv_list = sys.argv[1:] if argv is None else list(argv)
    args = _parse_args(argv_list)
    node_id, created = build(args.source, args.dag)
    status = "appended" if created else "unchanged"
    print(f"{status}: node={node_id[:8]}... at {args.dag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
