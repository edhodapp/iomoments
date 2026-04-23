"""DAG persistence and snapshot utilities for iomoments.

Forked 2026-04-23 from python_agent.dag_utils. HMAC integrity signing
and LLM prompt-injection scan are DROPPED: the iomoments ontology is
produced by a trusted in-repo builder, not loaded from external agent
output. If iomoments ever starts loading DAGs from a less-trusted
source, port the integrity/scan machinery back from python_agent or
from fireasmserver's equivalent fork.

Phase 1 landed baseline persistence; Phase 3 (this file) adds:

* ``ontology_content_hash`` — stable SHA-256 over a canonicalized
  Ontology so the builder can decide whether a new snapshot is a
  no-op repeat of its parent.
* ``snapshot_if_changed`` — idempotent wrapper around
  ``save_snapshot``; returns ``(node_id, created)`` so the caller
  knows whether a no-op fired.
* ``git_snapshot_label`` — timestamp + short HEAD SHA + ``+dirty``
  marker, so every DAG snapshot locates back to the source
  context in one ``git show``.
* ``dag_transaction`` — context manager that takes an advisory
  ``fcntl.flock`` on a sidecar lock file, loads, yields the DAG,
  and saves on normal exit. Parallel Claude sessions invoking the
  builder can't lose each other's updates.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

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
    the ontology actually changed" should use ``snapshot_if_changed``.
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


# -- Phase 3: content hashing, git labels, idempotent snapshot ------------


def _canonical_hash(model: BaseModel) -> str:
    """Canonicalize a pydantic model and return its SHA-256 hex digest.

    The canonicalization sorts keys recursively so two models with the
    same semantic content hash identically even when pydantic's
    field-declaration order drifts across schema refactors.

    Uses ``model_dump(mode="json")`` rather than plain ``model_dump()``
    so pydantic emits the canonical JSON encoding for types like
    ``datetime`` and ``UUID`` (ISO-string and str respectively) rather
    than leaking Python repr through ``default=str``, where naive vs
    tz-aware datetime stringification could silently drift the hash.

    **List order is semantic.** Two ontologies with the same
    ``domain_constraints`` in different order hash differently — the
    builder controls order deterministically, so this doubles as a
    "did someone reshuffle the list" signal. If authoring workflows
    ever want order-independent equivalence, hash a sorted view
    explicitly rather than weakening the canonical form here.
    """
    canonical = json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ontology_content_hash(ontology: Ontology) -> str:
    """SHA-256 hex digest over a single Ontology snapshot.

    Used by ``snapshot_if_changed`` to decide whether an append is a
    no-op. Stable across process invocations and across pydantic
    field-order refactors.
    """
    return _canonical_hash(ontology)


def _git_head_sha(short: bool = True) -> str | None:
    """Current HEAD SHA, or None if git is unavailable / out of repo.

    ``short=True`` yields the ~7-char form for human-readable labels;
    ``short=False`` yields the full 40-char SHA for cross-references.
    """
    args = ["git", "rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_is_dirty() -> bool:
    """True iff the working tree has uncommitted changes.

    ``git status --porcelain`` is empty when clean; any non-empty
    output means dirty. Errors / timeouts are treated as clean —
    under-flagging is preferable to crashing the builder on a git
    hiccup.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def git_snapshot_label(prefix: str = "") -> str:
    """Return a snapshot label embedding timestamp + git source context.

    Format: ``[<prefix> ]<ISO-UTC-timestamp>[ @<short-sha>[+dirty]]``.
    Outside a git checkout (or if git is unavailable) the SHA segment
    is omitted; the timestamp is always present.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha = _git_head_sha(short=True)
    parts = [prefix, now] if prefix else [now]
    if sha:
        dirty = "+dirty" if _git_is_dirty() else ""
        parts.append(f"@{sha}{dirty}")
    return " ".join(parts)


def snapshot_if_changed(
    dag: OntologyDAG,
    ontology: Ontology,
    label: str,
    decision: Decision | None = None,
) -> tuple[str, bool]:
    """Append a snapshot only when the ontology differs from the parent.

    Returns ``(node_id, created)``: ``created`` is True when a new
    node was appended, False when the content hash matched the
    current node and no append was needed. In the no-op case the
    returned id is the current node's, so callers always have a valid
    reference to "the node holding this ontology."

    Empty DAG (no current node) is the bootstrap case — always
    appends, regardless of hash.
    """
    new_hash = ontology_content_hash(ontology)
    current = dag.get_current_node()
    if current is not None:
        if ontology_content_hash(current.ontology) == new_hash:
            return current.id, False
    node_id = save_snapshot(dag, ontology, label, decision)
    return node_id, True


# -- Phase 3: advisory-locked transaction ---------------------------------


@contextmanager
def dag_transaction(
    path: str,
    project_name: str,
) -> Iterator[OntologyDAG]:
    """Process-safe load / modify / save transaction on a DAG file.

    Usage::

        with dag_transaction(path, project_name) as dag:
            snapshot_if_changed(dag, ontology, label)
        # DAG saved here; lock released.

    **Concurrency contract.** Two processes contending for the same
    DAG serialize: the second blocks on ``flock`` until the first's
    save completes and the with-block exits. No lost updates; no
    torn reads.

    **Rollback-on-exception.** An exception inside the yielded block
    (or from ``load_dag`` / ``save_dag`` themselves) short-circuits
    the save while the ``finally`` block releases the advisory lock.
    The on-disk DAG remains at the pre-transaction state.

    **Save-elision.** The transaction compares the DAG's pydantic
    state before-yield and after-yield; when unchanged it skips
    ``save_dag`` entirely. Regenerator runs that produce the same
    content don't rewrite the file or bump its mtime.

    **Concurrency limitations (same as fireasmserver's equivalent):**

    * Advisory lock only — a cooperating process that bypasses
      ``dag_transaction`` can still trample the file. Every builder
      must go through this wrapper.
    * Same-process nested calls on the same path self-deadlock; don't
      nest.
    * Linux-only (``fcntl.flock``). Target platforms are Linux only.

    The sidecar lock file at ``path + ".lock"`` is created lazily and
    persists across runs — creating and deleting it per transaction
    would open a TOCTOU race of its own.
    """
    lock_path = path + ".lock"
    parent_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent_dir, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            dag = load_dag(path, project_name)
            pre_state = dag.model_copy(deep=True)
            yield dag
            if dag != pre_state:
                save_dag(dag, path)
        finally:
            # Explicit release. The outer ``with open`` fd-close
            # would also release the advisory lock, but the finally
            # spells out the guarantee for any reader auditing the
            # concurrency story.
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
