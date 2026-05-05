"""D015 §2 ⊑ structural-subtyping operator for environments.

Used by both the freshness pass (does a TestResult's env match the
claim's expected env?) and the perf-budget pass (find TestResults
whose env matches the row's expected env). Same operator either way;
centralized so both passes stay lock-step on env semantics.
"""

from __future__ import annotations

from iomoments_ontology import EnvironmentSpec


_ENV_FIELDS = ("kind", "kernel", "distro", "arch")


def env_matches(actual: EnvironmentSpec, expected: EnvironmentSpec) -> bool:
    """True iff ``actual`` is a structural subtype of ``expected``.

    Per D015 §2's ``⊑`` operator: empty fields on the expected env
    match any value on the actual; non-empty fields must equal.
    """
    for field_name in _ENV_FIELDS:
        expected_v = getattr(expected, field_name)
        if expected_v and getattr(actual, field_name) != expected_v:
            return False
    for k, v in expected.flags.items():
        if actual.flags.get(k) != v:
            return False
    return True
