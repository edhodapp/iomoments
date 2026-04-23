"""Numerical reference for iomoments' Pébay update rules.

This is the test-side oracle called out in DECISIONS.md D005: a Python
implementation that validates the C kernel-side Pébay (Sandia
SAND2008-6212) computation against naive high-precision math and
scipy's descriptive statistics.

Current state: scaffolding only. One smoke test that proves the
numpy/scipy dev-dep chain is installed and callable. Real update-rule
validation lands as the C side grows (see D008).
"""

from __future__ import annotations

import numpy as np
from scipy import stats


def test_scipy_moments_round_trip() -> None:
    """scipy and numpy agree on the first two moments of a known sample.

    This is a pipeline smoke test, not a real oracle. It confirms that
    the dev-dep chain (numpy + scipy) resolves, is importable, and
    produces the textbook answer for a trivial case.
    """
    rng = np.random.default_rng(seed=0)
    sample = rng.normal(loc=3.0, scale=2.0, size=10_000)

    np_mean = float(np.mean(sample))
    scipy_mean = float(stats.tmean(sample))
    np_var = float(np.var(sample, ddof=0))
    scipy_var = float(stats.tvar(sample, ddof=0))

    # Tight bounds: identical input, identical dtype, no randomness here —
    # any divergence between numpy and scipy at this stage would mean a
    # library regression, not a numerical-method subtlety.
    assert abs(np_mean - scipy_mean) < 1e-12
    assert abs(np_var - scipy_var) < 1e-10
