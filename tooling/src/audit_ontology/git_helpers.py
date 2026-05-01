"""Git operations needed by the D015 freshness audit.

The freshness rule (D015 §2) needs two questions answered against
the host's git repository:

1. **Last-touch SHA per file**: ``git log -1 --format=%H HEAD -- <file>``
   tells us the most recent commit in HEAD's ancestry that touched
   the named file. The freshness rule uses this to determine whether
   a TestResult was captured at-or-after the last meaningful change
   to the claim's referenced files.

2. **Ancestry membership**: ``git merge-base --is-ancestor <sha> HEAD``
   tells us whether a recorded ``captured_git_sha`` is reachable from
   HEAD. A TestResult captured on a branch that was never merged
   doesn't count toward freshness on this branch.

These wrap subprocess calls with bounded timeouts and translate
git's exit codes into Python booleans / Optional[str]. Errors
(no git, not a repo, bad path) return None / False rather than
raising; the freshness layer treats a missing git as "no
authoritative answer" and surfaces it as an audit gap so the user
fixes the environment rather than silently ignoring the question.

Per D015 §2's mathematical rule ``≽`` semantics: "at-or-after, in
HEAD's ancestry" means BOTH ``is_ancestor_of_head(sha) is True``
AND ``not is_strictly_before(sha, last_touch_sha)``. This module
provides the two primitives; the composition is in
``audit_ontology.freshness``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_GIT_TIMEOUT_S = 5


def _run_git(
    repo_root: Path, args: list[str],
) -> tuple[int, str]:
    """Run a git command; return (returncode, stdout-stripped)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout.strip()


def head_sha(repo_root: Path) -> str | None:
    """Current HEAD SHA (full 40-char), or None if git unavailable."""
    rc, out = _run_git(repo_root, ["rev-parse", "HEAD"])
    if rc != 0 or not out:
        return None
    return out


def last_touch_file(repo_root: Path, file_relpath: str) -> str | None:
    """Last commit SHA in HEAD's ancestry that touched ``file_relpath``.

    Returns None if git is unavailable, the path is not under git
    control, or no commits in HEAD's ancestry have touched the file.
    The freshness layer treats None as "no authoritative answer" and
    surfaces a gap; the user is expected to ensure the file is
    git-tracked.
    """
    rc, out = _run_git(
        repo_root,
        ["log", "-1", "--format=%H", "HEAD", "--", file_relpath],
    )
    if rc != 0 or not out:
        return None
    return out


def is_ancestor_of_head(repo_root: Path, sha: str) -> bool:
    """True iff ``sha`` is reachable from HEAD via parent edges.

    Used by the freshness rule to verify that a TestResult's
    ``captured_git_sha`` is actually on the current branch, not on a
    sibling branch that was never merged. ``git merge-base
    --is-ancestor`` exits 0 for ancestors, 1 for non-ancestors,
    other for errors. We treat anything non-zero as "not ancestor"
    (false-negatives over false-positives — the safer direction for
    an audit that's checking "did this test cover the current
    code").
    """
    rc, _ = _run_git(
        repo_root, ["merge-base", "--is-ancestor", sha, "HEAD"],
    )
    return rc == 0


def is_at_or_after(
    repo_root: Path, sha_candidate: str, sha_reference: str,
) -> bool:
    """True iff ``sha_candidate`` is at-or-after ``sha_reference`` in
    HEAD's ancestry.

    "At-or-after" matches D015 §2's ``≽`` operator: either the same
    commit (sha_candidate == sha_reference) or sha_reference is an
    ancestor of sha_candidate (sha_candidate landed later on the
    same chain). Both must also be in HEAD's ancestry — a candidate
    on an unmerged branch fails even if it's later in wall-clock
    time.

    Implementation: sha_candidate must be in HEAD's ancestry AND
    sha_reference must be an ancestor of sha_candidate (or equal).
    The second clause is the at-or-after part: ``git merge-base
    --is-ancestor sha_reference sha_candidate``.
    """
    if sha_candidate == sha_reference:
        return is_ancestor_of_head(repo_root, sha_candidate)
    if not is_ancestor_of_head(repo_root, sha_candidate):
        return False
    rc, _ = _run_git(
        repo_root,
        ["merge-base", "--is-ancestor", sha_reference, sha_candidate],
    )
    return rc == 0
