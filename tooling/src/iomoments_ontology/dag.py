"""DAG persistence and snapshot utilities for iomoments.

Forked 2026-04-23 from python_agent.dag_utils. HMAC integrity signing
and LLM prompt-injection scan are DROPPED: the iomoments ontology is
produced by a trusted in-repo builder, not loaded from external agent
output. If iomoments ever starts loading DAGs from a less-trusted
source, port the integrity/scan machinery back from python_agent or
from fireasmserver's equivalent fork.

Phase 1 (this commit): baseline persistence only — load_dag, save_dag,
make_node_id, save_snapshot. The fireasmserver-style extensions
(content-hash idempotent snapshot, git-SHA labels, fcntl.flock
dag_transaction) land in Phase 3 per D009.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

from iomoments_ontology.models import (
    DAGEdge,
    DAGNode,
    Decision,
    Ontology,
    OntologyDAG,
)


def make_node_id() -> str:
    """Generate a unique node ID using uuid4."""
    return str(uuid.uuid4())


def load_dag(path: str, project_name: str) -> OntologyDAG:
    """Load an OntologyDAG from JSON, or return an empty new DAG.

    A validation failure is treated as a hard error (raises): the
    builder should never silently replace a corrupted DAG with an
    empty one, since the DAG is iomoments' formal-requirements
    artifact. File-not-found is the only silent-empty path.
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return OntologyDAG(project_name=project_name)
    return OntologyDAG.from_json(text)


def _cleanup_tempfile(fd: Any, name: str) -> None:
    """Best-effort close + unlink; swallow everything so the caller's
    original exception is the one that propagates."""
    try:
        fd.close()
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        os.unlink(name)
    except FileNotFoundError:
        pass


def save_dag(dag: OntologyDAG, path: str) -> None:
    """Persist an OntologyDAG to JSON via atomic tempfile + rename.

    Missing parent directories are created. The rename is ``os.replace``
    for cross-platform atomicity; on failure the tempfile is cleaned up
    and the original exception re-raised (cleanup errors are swallowed
    so they can't mask the root cause).
    """
    parent_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent_dir, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(
        mode="w",
        dir=parent_dir,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    )
    try:
        fd.write(dag.to_json())
        fd.close()
        os.replace(fd.name, path)
    except BaseException:
        _cleanup_tempfile(fd, fd.name)
        raise


def save_snapshot(
    dag: OntologyDAG,
    ontology: Ontology,
    label: str,
    decision: Decision | None = None,
) -> str:
    """Append a new DAG node carrying `ontology`.

    Links the new node as a child of the current node when one exists.
    When the parent edge is created without a caller-supplied decision,
    a placeholder Decision is synthesized so the audit trail is never
    ragged. Returns the new node id.

    This unconditionally appends — callers that need "only append if
    the ontology actually changed" must use the Phase 3
    ``snapshot_if_changed`` wrapper (landing with D009 P3).
    """
    now = datetime.now(timezone.utc).isoformat()
    node_id = make_node_id()
    node = DAGNode(
        id=node_id,
        ontology=ontology.model_copy(deep=True),
        created_at=now,
        label=label,
    )
    dag.nodes.append(node)
    if dag.current_node_id:
        if decision is None:
            decision = Decision(
                question="save",
                options=["continue"],
                chosen="continue",
                rationale=label,
            )
        edge = DAGEdge(
            parent_id=dag.current_node_id,
            child_id=node_id,
            decision=decision,
            created_at=now,
        )
        dag.edges.append(edge)
    dag.current_node_id = node_id
    return node_id
