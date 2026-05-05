"""D017 perf-budget check: does each implemented PerformanceConstraint's
latest measurement satisfy its budget?

Per D017 §Decision: a PerformanceConstraint at ``status="implemented"``
or ``status="tested"`` has a contract with the audit:

    For every E ∈ expected_environments(c).
        ∃ R ∈ test_results.
          R.environment ⊑ E ∧
          R.outcome = pass ∧
          c.metric ∈ R.measurements ∧
          direction(R.measurements[c.metric], c.budget)

A failure is reported as one of two modes:

* ``BUDGET_VIOLATED``: a TestResult with the metric exists for the
  expected env, but its value violates ``direction(budget)``.
* ``NO_MEASUREMENT``: no TestResult contains the metric for the
  expected env. Distinct from STALE_RESULT (which the freshness
  pass reports separately) — perf-budget asks "is there a
  measurement to compare?", freshness asks "is the measurement
  fresh enough to count?". A row failing both passes surfaces in
  both, with different fix recipes.

Spec / deviation / n_a status: skipped entirely. Promotion to
``implemented`` is the act that opts a row into the budget gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from audit_ontology.env_match import env_matches
from iomoments_ontology import (
    EnvironmentSpec,
    PerformanceConstraint,
    TestResult,
    TestResultsSnapshot,
)


class PerfBudgetMode(str, Enum):
    """Which D017 failure mode applies to this gap."""

    BUDGET_VIOLATED = "budget_violated"
    NO_MEASUREMENT = "no_measurement"


@dataclass(frozen=True)
class PerfBudgetIssue:
    """One (claim, env) gap surfaced by the perf-budget audit."""

    claim_name: str
    metric: str
    budget: float
    measured: float | None
    direction: str
    unit: str
    environment: EnvironmentSpec
    mode: PerfBudgetMode
    reason: str
    fix_recipe: str


def _direction_satisfied(
    measured: float, budget: float, direction: str,
) -> bool:
    """True iff ``measured`` satisfies ``direction(budget)``.

    ``direction`` matches PerfDirection: "max" / "min" / "equal".
    Unknown direction fails closed (the audit refuses to lie about
    a row whose comparison rule it doesn't recognize).
    """
    if direction == "max":
        return measured <= budget
    if direction == "min":
        return measured >= budget
    if direction == "equal":
        return measured == budget
    return False


def _latest_matching_result(
    snapshot: TestResultsSnapshot,
    metric: str,
    expected_env: EnvironmentSpec,
) -> TestResult | None:
    """Latest passing TestResult whose env matches and measurements has metric.

    Latest = most recent ``captured_at``. Within-snapshot retention
    (D015 §4: latest-passing-per-(verification_ref, environment))
    means at most one match per producer per env, but the perf-
    budget pass keys on ``metric`` not on ``verification_ref`` —
    multiple producers could in principle emit the same metric.
    Take the most recent across all of them.
    """
    candidates = [
        r for r in snapshot.results
        if r.outcome == "pass"
        and env_matches(r.environment, expected_env)
        and metric in r.measurements
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.captured_at)


def _build_issue(
    claim: PerformanceConstraint,
    measured: float | None,
    env: EnvironmentSpec,
    mode: PerfBudgetMode,
    reason: str,
) -> PerfBudgetIssue:
    return PerfBudgetIssue(
        claim_name=claim.name,
        metric=claim.metric,
        budget=claim.budget,
        measured=measured,
        direction=claim.direction,
        unit=claim.unit,
        environment=env,
        mode=mode,
        reason=reason,
        fix_recipe=env.fix_recipe,
    )


def _check_one_env(
    claim: PerformanceConstraint,
    env: EnvironmentSpec,
    snapshot: TestResultsSnapshot,
) -> PerfBudgetIssue | None:
    """Apply the rule for one (claim, env) pair, return one issue or None."""
    result = _latest_matching_result(snapshot, claim.metric, env)
    if result is None:
        return _build_issue(
            claim, None, env, PerfBudgetMode.NO_MEASUREMENT,
            f"no passing TestResult contains "
            f"measurements[{claim.metric!r}] in this environment",
        )
    measured = result.measurements[claim.metric]
    if _direction_satisfied(measured, claim.budget, claim.direction):
        return None
    return _build_issue(
        claim, measured, env, PerfBudgetMode.BUDGET_VIOLATED,
        f"measured {measured:g} {claim.unit} violates "
        f"{claim.direction} {claim.budget:g} {claim.unit}",
    )


def check_perf_budget(
    claim: PerformanceConstraint,
    snapshot: TestResultsSnapshot,
) -> list[PerfBudgetIssue]:
    """Apply the perf-budget rule to one PerformanceConstraint."""
    if claim.status not in ("implemented", "tested"):
        return []
    issues: list[PerfBudgetIssue] = []
    for env in claim.expected_environments:
        issue = _check_one_env(claim, env, snapshot)
        if issue is not None:
            issues.append(issue)
    return issues
