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
    TestResult,
    TestResultsDAG,
    TestResultsDAGNode,
    TestResultsSnapshot,
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


_DEFAULT_KEEP_LAST_K = 100  # D015 §4: cross-snapshot retention default


# -- D015: TestResultsDAG persistence (mirrors OntologyDAG above) ---------


def load_test_results_dag(
    path: str, project_name: str,
) -> TestResultsDAG:
    """Load a TestResultsDAG from JSON, or return an empty new DAG.

    File-not-found returns an empty DAG (the bootstrap case);
    parse failures raise (corrupted DAG must not be silently
    replaced — the audit reads from this).
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return TestResultsDAG(project_name=project_name)
    return TestResultsDAG.from_json(text)


def save_test_results_dag(dag: TestResultsDAG, path: str) -> None:
    """Persist a TestResultsDAG via atomic tempfile + rename."""
    parent_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent_dir, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(
        mode="w", dir=parent_dir, suffix=".tmp",
        delete=False, encoding="utf-8",
    )
    try:
        fd.write(dag.to_json())
        fd.close()
        os.replace(fd.name, path)
    except BaseException:
        _cleanup_tempfile(fd, fd.name)
        raise


def test_results_content_hash(snapshot: TestResultsSnapshot) -> str:
    """SHA-256 hex digest over a TestResultsSnapshot.

    Used by ``snapshot_test_results_if_changed`` to decide whether
    an append is a no-op. Same canonicalization rules as
    ``ontology_content_hash``: list order is semantic, datetime
    fields use ISO-string encoding, no Python repr leakage.
    """
    return _canonical_hash(snapshot)


def save_test_results_snapshot(
    dag: TestResultsDAG,
    snapshot: TestResultsSnapshot,
    label: str,
    decision: Decision | None = None,
) -> str:
    """Append a new node carrying ``snapshot`` to the DAG.

    Mirrors ``save_snapshot`` for OntologyDAG: links the new node as
    a child of the current node when one exists; synthesizes a
    placeholder Decision if the caller doesn't supply one. Returns
    the new node id.

    Unconditional append — for content-hash-gated append, use
    ``snapshot_test_results_if_changed``.
    """
    now = datetime.now(timezone.utc).isoformat()
    node_id = make_node_id()
    node = TestResultsDAGNode(
        id=node_id,
        snapshot=snapshot.model_copy(deep=True),
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


def snapshot_test_results_if_changed(
    dag: TestResultsDAG,
    snapshot: TestResultsSnapshot,
    label: str,
    decision: Decision | None = None,
) -> tuple[str, bool]:
    """Append a snapshot only when content differs from the parent.

    Returns ``(node_id, created)``: ``created`` is True when a new
    node was appended, False when the content hash matched and no
    append was needed. Empty DAG (bootstrap) always appends.
    """
    new_hash = test_results_content_hash(snapshot)
    current = dag.get_current_node()
    if current is not None:
        if test_results_content_hash(current.snapshot) == new_hash:
            return current.id, False
    node_id = save_test_results_snapshot(dag, snapshot, label, decision)
    return node_id, True


def prune_and_add_result(
    snapshot: TestResultsSnapshot,
    new_result: TestResult,
) -> TestResultsSnapshot:
    """Return a new snapshot with ``new_result`` replacing any older
    record sharing the same (verification_ref, env.natural_key()).

    D015 §4 within-snapshot retention: latest-passing-per-(ref, env).
    The TestResultsSnapshot validator already rejects duplicates;
    this helper does the pre-validation pruning so the producer
    pattern (load → mutate → save) is one line.
    """
    new_key = (
        new_result.verification_ref,
        new_result.environment.natural_key(),
    )
    pruned = [
        r for r in snapshot.results
        if (r.verification_ref, r.environment.natural_key()) != new_key
    ]
    pruned.append(new_result)
    return TestResultsSnapshot(results=pruned)


def prune_test_results_dag_nodes(
    dag: TestResultsDAG,
    keep_last_k: int = _DEFAULT_KEEP_LAST_K,
) -> int:
    """Drop ancestor nodes beyond the most recent K, return count pruned.

    D015 §4 across-snapshot retention. The current node is always
    kept; pruning walks the parent chain from current and keeps the
    most recent K-1 ancestors. Edges into pruned nodes are dropped
    too. Audit only reads the current snapshot, so pruning ancient
    history doesn't affect correctness.

    Disconnected nodes (not in the current node's ancestry) are
    NOT touched — this function is conservative; it only prunes
    what's safely on the chain to the current node.
    """
    if keep_last_k < 1:
        raise ValueError(f"keep_last_k must be >= 1, got {keep_last_k}")
    if not dag.current_node_id:
        return 0

    parent_lookup: dict[str, str] = {
        e.child_id: e.parent_id for e in dag.edges
    }
    chain: list[str] = []
    node_id: str | None = dag.current_node_id
    while node_id is not None:
        chain.append(node_id)
        node_id = parent_lookup.get(node_id)

    if len(chain) <= keep_last_k:
        return 0

    keep_ids = set(chain[:keep_last_k])
    drop_ids = set(chain[keep_last_k:])

    before_node_count = len(dag.nodes)
    dag.nodes = [n for n in dag.nodes if n.id not in drop_ids]
    dag.edges = [
        e for e in dag.edges
        if e.parent_id in keep_ids and e.child_id in keep_ids
    ]
    return before_node_count - len(dag.nodes)


@contextmanager
def test_results_dag_transaction(
    path: str,
    project_name: str,
) -> Iterator[TestResultsDAG]:
    """Process-safe load / modify / save transaction for TestResultsDAG.

    Mirrors ``dag_transaction`` for OntologyDAG: advisory fcntl
    lock, save-elision when state unchanged, rollback on exception.
    Lock file lives at ``path + ".lock"``; same TOCTOU-avoidance
    discipline as the ontology transaction.

    Producer usage::

        with test_results_dag_transaction(path, project_name) as dag:
            current = dag.get_current_node()
            base = current.snapshot if current else TestResultsSnapshot()
            updated = prune_and_add_result(base, new_result)
            snapshot_test_results_if_changed(dag, updated, label)
        # DAG saved here; lock released.
    """
    lock_path = path + ".lock"
    parent_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent_dir, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            dag = load_test_results_dag(path, project_name)
            pre_state = dag.model_copy(deep=True)
            yield dag
            if dag != pre_state:
                save_test_results_dag(dag, path)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


# -- Original OntologyDAG transaction (unchanged) -------------------------


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
