"""D015 freshness check: is each claim's `tested` status currently true?

Implements the mathematical rule from D015 §2:

    A claim c is `tested` at HEAD iff
      ∀ T ∈ verification_refs(c). ∀ E ∈ expected_environments(c).
        ∃ R ∈ test_results.
          R.verification_ref = T ∧
          R.environment ⊑ E ∧
          R.outcome = pass ∧
          R.captured_git_sha ∈ ancestry(HEAD) ∧
          R.captured_git_sha ≽ max{ last_touch(f) :
                                    f ∈ files(impl_refs(c) ∪
                                              verification_refs(c)) }

A failure is reported as one of four modes per D015 §5:

* ``STALE_RESULT``: a TestResult exists for (T, E) but its
  ``captured_git_sha`` is older than the impl's last_touch — code
  edited, test never re-ran in the relevant environment.
* ``RUNNER_FORGOT``: no TestResult exists for (T, E), but a
  TestResult exists for T in some other env. The producer fires
  for this ref but didn't fire for the required env.
* ``ENV_NEVER_EXERCISED``: no TestResult exists for T in any env.
  No producer has ever emitted for this ref.
* ``UNTRACKED_FILE``: the claim references a file not under git
  control, so no last_touch SHA can be computed and the freshness
  rule cannot be applied. Surfaces because "no answer" must not
  silently pass — D015 demands the audit refuse to lie about
  itself, and we cannot verify a TestResult is fresher than a
  file we can't pin a SHA to.

Distinguishing STALE_RESULT (old SHA) from RUNNER_FORGOT (no
result for this env) from ENV_NEVER_EXERCISED (no result anywhere)
guides the right fix: re-run the existing test, wire the producer
to fire for this env, or wire a producer for the ref at all.
UNTRACKED_FILE points at a different fix: track the file in git.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from audit_ontology import git_helpers
from audit_ontology.env_match import env_matches
from audit_ontology.parser import parse_ref
from iomoments_ontology import (
    EnvironmentSpec,
    TestResult,
    TestResultsSnapshot,
)


class FreshnessMode(str, Enum):
    """Which D015 §5 failure mode applies to this gap."""

    RUNNER_FORGOT = "runner_forgot"
    STALE_RESULT = "stale_result"
    ENV_NEVER_EXERCISED = "env_never_exercised"
    UNTRACKED_FILE = "untracked_file"


@dataclass(frozen=True)
class FreshnessIssue:
    """One (claim, ref, env) gap surfaced by the freshness audit."""

    claim_kind: str
    claim_name: str
    verification_ref: str
    environment: EnvironmentSpec
    mode: FreshnessMode
    reason: str
    fix_recipe: str


def _matching_results(
    snapshot: TestResultsSnapshot,
    verification_ref: str,
    expected_env: EnvironmentSpec,
) -> list[TestResult]:
    """All passing TestResults matching (verification_ref, expected_env)."""
    return [
        r for r in snapshot.results
        if r.verification_ref == verification_ref
        and r.outcome == "pass"
        and env_matches(r.environment, expected_env)
    ]


def _claim_files(claim: Any) -> list[str]:
    """File paths from the claim's impl_refs ∪ verification_refs.

    Empty refs list yields empty file list (the freshness rule's
    max{} reduces to "no constraint"; any captured_git_sha satisfies).
    """
    files: set[str] = set()
    for ref_str in (
        list(claim.implementation_refs) + list(claim.verification_refs)
    ):
        ref_str = ref_str.strip()
        if not ref_str:
            continue
        try:
            parsed = parse_ref(ref_str)
        except ValueError:
            continue
        files.add(parsed.path)
    return sorted(files)


def _resolve_last_touches(
    repo_root: Path, files: list[str],
) -> tuple[list[str], list[str]]:
    """Return (resolved_shas, unresolved_files) for the named files.

    Resolved: last-touch SHA known. Unresolved: file isn't under git
    control (no commit ever touched it) or git is unavailable. The
    distinction matters — empty files list means "no constraint"
    while non-empty unresolved means "we cannot answer, surface as
    a gap."
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    for f in files:
        sha = git_helpers.last_touch_file(repo_root, f)
        if sha:
            resolved.append(sha)
        else:
            unresolved.append(f)
    return resolved, unresolved


def _classify_no_match(
    snapshot: TestResultsSnapshot,
    verification_ref: str,
) -> FreshnessMode:
    """When no result matches (ref, env), decide which §5 mode it is.

    If ANY past TestResult exists for the verification_ref (in any
    env) → RUNNER_FORGOT (the producer fires for this ref; just not
    in the required env this time).
    Otherwise → ENV_NEVER_EXERCISED (no producer has ever emitted
    for this ref).
    """
    for r in snapshot.results:
        if r.verification_ref == verification_ref:
            return FreshnessMode.RUNNER_FORGOT
    return FreshnessMode.ENV_NEVER_EXERCISED


def _build_issue(
    claim_kind: str,
    claim_name: str,
    verification_ref: str,
    expected_env: EnvironmentSpec,
    mode: FreshnessMode,
    reason: str,
) -> FreshnessIssue:
    return FreshnessIssue(
        claim_kind=claim_kind,
        claim_name=claim_name,
        verification_ref=verification_ref,
        environment=expected_env,
        mode=mode,
        reason=reason,
        fix_recipe=expected_env.fix_recipe,
    )


def _claim_label(claim: Any) -> str:
    """Best-effort name for a claim (matches audit.py's pattern).

    DomainConstraint / PerformanceConstraint / DiagnosticSignal carry
    a ``name`` field; VerdictNode keys on ``kind`` (one node per
    VerdictKind value). Mirroring the same pattern used in
    audit._build_row so output is consistent across paths.
    """
    if hasattr(claim, "name"):
        return str(claim.name)
    return str(claim.kind)


def check_claim_freshness(
    claim_kind: str,
    claim: Any,
    snapshot: TestResultsSnapshot,
    repo_root: Path,
) -> list[FreshnessIssue]:
    """Apply the freshness rule to one claim, return any gaps."""
    if claim.status != "tested":
        return []
    files = _claim_files(claim)
    last_touches, unresolved = _resolve_last_touches(repo_root, files)
    claim_name = _claim_label(claim)

    if unresolved:
        return _untracked_file_issues(
            claim_kind, claim_name, claim, unresolved,
        )

    issues: list[FreshnessIssue] = []
    for ref_str in claim.verification_refs:
        for env in claim.expected_environments:
            issues.extend(_check_one_pair(
                claim_kind, claim_name, ref_str, env,
                snapshot, repo_root, last_touches,
            ))
    return issues


def _untracked_file_issues(
    claim_kind: str,
    claim_name: str,
    claim: Any,
    unresolved: list[str],
) -> list[FreshnessIssue]:
    """One UNTRACKED_FILE issue per (verification_ref, env) pair.

    Reported per-pair (rather than once per claim) so the formatter's
    existing per-issue rendering Just Works. The reason text names
    the unresolved files so the user knows which to git-add.
    """
    issues: list[FreshnessIssue] = []
    files_str = ", ".join(unresolved)
    reason = (
        f"claim references files not under git control: {files_str}; "
        f"freshness rule cannot be applied"
    )
    for ref_str in claim.verification_refs:
        for env in claim.expected_environments:
            issues.append(_build_issue(
                claim_kind, claim_name, ref_str, env,
                FreshnessMode.UNTRACKED_FILE, reason,
            ))
    return issues


def _is_fresh_result(
    repo_root: Path,
    result_sha: str,
    last_touches: list[str],
) -> bool:
    """True iff result_sha satisfies the freshness rule for ALL last-touches.

    Per D015 §2's ``≽`` over ``max{last_touch(f) : f ∈ files}``: the
    set semantics demand the result SHA be at-or-after EVERY
    last-touch (not just one of them). On linear histories this
    reduces to "at-or-after the most recent last-touch"; on
    non-linear histories with merges, the per-file last_touches may
    be incomparable, so the ALL form is the correct one.
    """
    if not git_helpers.is_ancestor_of_head(repo_root, result_sha):
        return False
    for lt in last_touches:
        if not git_helpers.is_at_or_after(repo_root, result_sha, lt):
            return False
    return True


def _check_one_pair(
    claim_kind: str,
    claim_name: str,
    ref_str: str,
    env: EnvironmentSpec,
    snapshot: TestResultsSnapshot,
    repo_root: Path,
    last_touches: list[str],
) -> list[FreshnessIssue]:
    """Check freshness for a single (verification_ref, env) pair."""
    matches = _matching_results(snapshot, ref_str, env)
    if not matches:
        mode = _classify_no_match(snapshot, ref_str)
        reason = (
            f"no passing TestResult for verification_ref={ref_str!r} "
            f"in this environment"
        )
        return [_build_issue(
            claim_kind, claim_name, ref_str, env, mode, reason,
        )]

    fresh = [
        r for r in matches
        if _is_fresh_result(repo_root, r.captured_git_sha, last_touches)
    ]
    if fresh:
        return []

    # Have a result but it's stale. Pick the most-recent by
    # captured_at for the error message — matches list order from
    # snapshot.results is producer-write order and may not be
    # chronological under future producers.
    most_recent = max(matches, key=lambda r: r.captured_at)
    last_touch_repr = (
        ", ".join(lt[:12] for lt in last_touches)
        if last_touches else "unknown"
    )
    reason = (
        f"latest TestResult for verification_ref={ref_str!r} "
        f"captured at {most_recent.captured_git_sha[:12]} "
        f"is not at-or-after every impl-ref last-touch "
        f"({last_touch_repr})"
    )
    return [_build_issue(
        claim_kind, claim_name, ref_str, env,
        FreshnessMode.STALE_RESULT, reason,
    )]
