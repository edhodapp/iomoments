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

A failure is reported as one of three modes per D015 §5:

* ``RUNNER_FORGOT``: a TestResult exists for (T, E) but its
  ``captured_git_sha`` precedes the impl's last_touch — code edited,
  test never re-ran.
* ``STALE_RESULT``: a TestResult exists at the right SHA but with
  a different ``environment`` than required, OR no TestResult
  exists for (T, E) at all yet the producer should have emitted one.
* ``ENV_NEVER_EXERCISED``: no TestResult exists for (T, E) at any
  SHA, suggesting the producer for E hasn't been wired up yet.

The "RUNNER_FORGOT" vs "ENV_NEVER_EXERCISED" distinction is what
guides the user toward the right fix: did they need to re-run an
existing test (forgot), or wire up a producer for an environment
they've never tested in (never_exercised)? The distinction is
made by checking whether ANY past TestResult exists for (T, E):
yes → forgot/stale; no → never_exercised.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from audit_ontology import git_helpers
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


_ENV_FIELDS = ("kind", "kernel", "distro", "arch")


def _env_matches(actual: EnvironmentSpec, expected: EnvironmentSpec) -> bool:
    """Return True iff ``actual`` is a structural subtype of ``expected``.

    Per D015 §2's ``⊑`` operator: empty fields on the expected env
    match any value on the actual; non-empty fields must equal.
    Encoding the standard structural-subtyping rule used by the
    audit's per-(ref, env) lookup.
    """
    for field_name in _ENV_FIELDS:
        expected_v = getattr(expected, field_name)
        if expected_v and getattr(actual, field_name) != expected_v:
            return False
    for k, v in expected.flags.items():
        if actual.flags.get(k) != v:
            return False
    return True


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
        and _env_matches(r.environment, expected_env)
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
) -> list[str]:
    """Return last-touch SHA for each file that resolves; skip the rest."""
    out: list[str] = []
    for f in files:
        sha = git_helpers.last_touch_file(repo_root, f)
        if sha:
            out.append(sha)
    return out


def _pick_latest_sha(repo_root: Path, shas: list[str]) -> str:
    """Return the most-recent SHA via pairwise at-or-after comparison.

    n is small (~handful of refs per claim) so the O(n²) loop is
    fine and avoids needing a topological sort over commits.
    """
    latest = shas[0]
    for sha in shas[1:]:
        if git_helpers.is_at_or_after(repo_root, sha, latest):
            latest = sha
    return latest


def _max_last_touch_sha(
    repo_root: Path, files: list[str],
) -> str | None:
    """Latest commit-SHA touching any of the named files.

    Returns None if files is empty (no constraint) or if no file's
    last-touch can be resolved (git unavailable or all files
    untracked). The freshness checker treats a None last-touch as
    "no constraint" — any captured_git_sha is fresh.
    """
    if not files:
        return None
    last_touches = _resolve_last_touches(repo_root, files)
    if not last_touches:
        return None
    if len(last_touches) == 1:
        return last_touches[0]
    return _pick_latest_sha(repo_root, last_touches)


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
    issues: list[FreshnessIssue] = []
    files = _claim_files(claim)
    last_touch = _max_last_touch_sha(repo_root, files)
    claim_name = _claim_label(claim)

    for ref_str in claim.verification_refs:
        for env in claim.expected_environments:
            issues.extend(_check_one_pair(
                claim_kind, claim_name, ref_str, env,
                snapshot, repo_root, last_touch,
            ))
    return issues


def _check_one_pair(
    claim_kind: str,
    claim_name: str,
    ref_str: str,
    env: EnvironmentSpec,
    snapshot: TestResultsSnapshot,
    repo_root: Path,
    last_touch: str | None,
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

    # At least one result matches. Pick the most recent in HEAD's
    # ancestry that is at-or-after last_touch.
    fresh = [
        r for r in matches
        if git_helpers.is_ancestor_of_head(
            repo_root, r.captured_git_sha,
        )
        and (
            last_touch is None
            or git_helpers.is_at_or_after(
                repo_root, r.captured_git_sha, last_touch,
            )
        )
    ]
    if fresh:
        return []
    # Have a result but it's stale.
    most_recent = matches[-1]
    reason = (
        f"latest TestResult for verification_ref={ref_str!r} "
        f"captured at {most_recent.captured_git_sha[:12]} "
        f"precedes the impl's last edit "
        f"at {(last_touch or \"unknown\")[:12]}"
    )
    return [_build_issue(
        claim_kind, claim_name, ref_str, env,
        FreshnessMode.STALE_RESULT, reason,
    )]
