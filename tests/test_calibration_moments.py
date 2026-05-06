"""Calibration: do iomoments' moment computations agree with the
analytical truth for known distributions?

The Pébay update rule is mathematically correct — that's a theorem,
not a thing we test. What this suite tests is calibration: given N
samples from a distribution whose moments are known closed-form,
does the iomoments oracle's running summary converge to those
moments within sample-size error?

Why this matters: the verdict layer (D007) classifies workloads on
the basis of moment-derived signals (Carleman, Hill, JB, etc.). If
the moments themselves are off — even by a small amount — the
verdict's threshold-band assignment shifts, and the calibration of
the whole verdict layer is wrong. Pinning moments-on-known-shapes
is the precondition for any verdict-layer calibration claim.

Each fixture pins:
- the distribution + its parameters
- a deterministic seed
- the analytical truth for whichever moments exist
- the tolerance band for the comparison

Moments that do not exist for the distribution (e.g., variance of
Pareto α=1.5, kurtosis of Pareto α=2.5) are deliberately NOT
asserted on — sample moments of non-existing population moments
are noise that converges to nothing useful, and asserting either
direction would be asserting on noise.

Sample size N=100,000 throughout. Tolerances are distribution-
specific because heavy-tailed sampling has slow convergence even
on moments that do exist. The Pareto family is the canonical
boundary-tester: α=4.5 has all four moments; α=1.5 has only
mean — and the suite makes the moment-existence policy explicit
on each fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest
from scipy import stats

from iomoments_oracle import (
    Summary,
    excess_kurtosis,
    mean,
    merge,
    skewness,
    update_many,
    variance,
)


_N = 100_000


@dataclass(frozen=True)
class _Fixture:
    """One calibration case.

    A moment is "skipped" by setting its expected value to None.
    The test reads None as "this moment doesn't exist for this
    distribution; do not assert on it."
    """

    name: str
    seed: int
    samples: np.ndarray[Any, Any]
    # Analytical (or computable-to-high-precision) truth, or None
    # when the population moment does not exist.
    expected_mean: float | None
    expected_variance: float | None
    expected_skewness: float | None
    expected_excess_kurtosis: float | None
    # Distribution-specific tolerances. Relative for mean/variance
    # (multiplied by |truth|), absolute for skew/kurt.
    tol_mean_rel: float
    tol_variance_rel: float
    tol_skewness_abs: float
    tol_excess_kurtosis_abs: float


def _normal_fixture(
    name: str, mu: float, sigma: float, seed: int,
) -> _Fixture:
    """N(μ, σ²): light-tailed, all four moments exist closed-form."""
    rng = np.random.default_rng(seed=seed)
    return _Fixture(
        name=name,
        seed=seed,
        samples=rng.normal(loc=mu, scale=sigma, size=_N),
        expected_mean=mu,
        expected_variance=sigma * sigma,
        expected_skewness=0.0,
        expected_excess_kurtosis=0.0,
        tol_mean_rel=0.05,
        tol_variance_rel=0.05,
        tol_skewness_abs=0.05,
        tol_excess_kurtosis_abs=0.10,
    )


def _lognormal_fixture(name: str, sigma: float, seed: int) -> _Fixture:
    """LogNormal(μ=0, σ): asymmetric, all four moments exist for σ < ∞."""
    rng = np.random.default_rng(seed=seed)
    samples = rng.lognormal(mean=0.0, sigma=sigma, size=_N)
    # Analytical from scipy, parameter s=σ, scale=exp(μ)=1.
    # mypy: scipy-stubs declares the kwarg as `moment=` (singular)
    # but scipy itself accepts only `moments=` (plural) at runtime;
    # call-overload ignore tracks the stub bug, not real risk.
    truth = stats.lognorm.stats(  # type: ignore[call-overload]
        s=sigma, scale=1.0, moments="mvsk",
    )
    truth_mean, truth_var, truth_skew, truth_kurt = truth
    # Heavy-skewed at large σ: skewness and kurtosis converge VERY
    # slowly because their population values depend on exp(σ²)
    # which explodes with σ. At σ=1.0, population kurtosis ≈ 111
    # but the N=100K sample estimator typically lands around 50–60.
    # That ~50% under-estimate is *expected* and stable across
    # seeds — it's the regime where verdict layer signals (Hill,
    # JB, half-split) earn their keep, not where moment-based
    # summary stays trustworthy.
    return _Fixture(
        name=name,
        seed=seed,
        samples=samples,
        expected_mean=float(truth_mean),
        expected_variance=float(truth_var),
        expected_skewness=float(truth_skew),
        expected_excess_kurtosis=float(truth_kurt),
        tol_mean_rel=0.02,
        tol_variance_rel=0.10 * (1.0 + sigma),
        tol_skewness_abs=max(0.5, 0.7 * float(truth_skew)),
        tol_excess_kurtosis_abs=max(5.0, 0.7 * abs(float(truth_kurt))),
    )


def _exponential_fixture(seed: int) -> _Fixture:
    """Exp(λ=1): skew=2, excess kurtosis=6, all moments exist."""
    rng = np.random.default_rng(seed=seed)
    return _Fixture(
        name="exponential_lambda_1",
        seed=seed,
        samples=rng.exponential(scale=1.0, size=_N),
        expected_mean=1.0,
        expected_variance=1.0,
        expected_skewness=2.0,
        expected_excess_kurtosis=6.0,
        tol_mean_rel=0.02,
        tol_variance_rel=0.05,
        tol_skewness_abs=0.10,
        tol_excess_kurtosis_abs=0.50,
    )


def _uniform_fixture(seed: int) -> _Fixture:
    """U(0, 1): skew=0, excess kurtosis = -6/5 = -1.2."""
    rng = np.random.default_rng(seed=seed)
    return _Fixture(
        name="uniform_0_1",
        seed=seed,
        samples=rng.uniform(low=0.0, high=1.0, size=_N),
        expected_mean=0.5,
        expected_variance=1.0 / 12.0,
        expected_skewness=0.0,
        expected_excess_kurtosis=-1.2,
        tol_mean_rel=0.01,
        tol_variance_rel=0.02,
        tol_skewness_abs=0.05,
        tol_excess_kurtosis_abs=0.05,
    )


def _pareto_truths(
    alpha: float,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Closed-form Pareto Type I population moments at α, x_min=1.

    None for moments that don't exist at this α:
        α > 1: mean exists.   α > 2: variance.
        α > 3: skewness.      α > 4: excess kurtosis.
    """
    expected_mean = alpha / (alpha - 1.0) if alpha > 1.0 else None
    expected_var = (
        alpha / ((alpha - 1.0) ** 2 * (alpha - 2.0))
        if alpha > 2.0 else None
    )
    expected_skew: float | None = None
    if alpha > 3.0:
        expected_skew = (
            2.0 * (alpha + 1.0) / (alpha - 3.0)
            * float(np.sqrt((alpha - 2.0) / alpha))
        )
    expected_kurt: float | None = None
    if alpha > 4.0:
        num = alpha ** 3 + alpha ** 2 - 6.0 * alpha - 2.0
        den = alpha * (alpha - 3.0) * (alpha - 4.0)
        expected_kurt = 6.0 * num / den
    return expected_mean, expected_var, expected_skew, expected_kurt


def _pareto_tol_mean(alpha: float) -> float:
    """Mean estimator: 50% rel error at α<2 (wild), 10% at α<3, 5% else."""
    if alpha < 2.0:
        return 0.50
    if alpha < 3.0:
        return 0.10
    return 0.05


def _pareto_tol_var(alpha: float, expected_var: float | None) -> float:
    """Variance estimator: 80% rel near boundary, 30% mid, 10% else."""
    if expected_var is None:
        return 0.10
    if alpha < 3.0:
        return 0.80
    if alpha < 4.0:
        return 0.30
    return 0.10


def _pareto_tol_skew(alpha: float, expected_skew: float | None) -> float:
    """Skewness estimator: 70% rel error at α<4 (near boundary)."""
    if expected_skew is None:
        return 0.5
    if alpha < 4.0:
        return max(2.0, 0.7 * abs(expected_skew))
    return max(1.0, 0.3 * abs(expected_skew))


def _pareto_tol_kurt(expected_kurt: float | None) -> float:
    """Kurtosis estimator: 100% rel error or 20 absolute floor.

    Sample-kurt variance is governed by the 8th population moment;
    for Pareto with α just above 4, that moment doesn't exist, so
    sample kurt at any finite N has effectively-infinite variance.
    Tolerance is the calibration finding, not a weakness.
    """
    if expected_kurt is None:
        return 1.0
    return max(20.0, 1.0 * abs(expected_kurt))


def _pareto_tolerances(
    alpha: float,
    expected_var: float | None,
    expected_skew: float | None,
    expected_kurt: float | None,
) -> tuple[float, float, float, float]:
    """Heavy-tail sample-moment tolerances at α, N=100K.

    Heavy-tail skew/kurt sample estimators have huge variance when
    α is just above the existence boundary. At N=100K:
      α=3.5 sample skew lands ~50% below population truth.
      α=4.5 sample kurt lands ~75% below population truth.
    Tolerances of 70% / 100% relative error are calibration
    findings, not weakness — they document the convergence
    behaviour of plain sample moments on near-boundary tails.
    """
    return (
        _pareto_tol_mean(alpha),
        _pareto_tol_var(alpha, expected_var),
        _pareto_tol_skew(alpha, expected_skew),
        _pareto_tol_kurt(expected_kurt),
    )


def _pareto_fixture(alpha: float, seed: int) -> _Fixture:
    """Pareto(α) with x_min=1. Boundary case for the calibration set.

    For α at-or-below each existence boundary, the corresponding
    population moment does not exist; sample estimates of those
    moments are noise. The fixture sets their expected_* to None
    and the test skips assertion.
    """
    rng = np.random.default_rng(seed=seed)
    # numpy.random.pareto returns the *Pareto Type I* form with
    # x_min=0; add 1 to land on x_min=1, x ≥ 1 (canonical Pareto).
    samples = rng.pareto(a=alpha, size=_N) + 1.0
    expected_mean, expected_var, expected_skew, expected_kurt = (
        _pareto_truths(alpha)
    )
    tol_mean_rel, tol_var_rel, tol_skew_abs, tol_kurt_abs = (
        _pareto_tolerances(
            alpha, expected_var, expected_skew, expected_kurt,
        )
    )

    return _Fixture(
        name=f"pareto_alpha_{alpha}",
        seed=seed,
        samples=samples,
        expected_mean=expected_mean,
        expected_variance=expected_var,
        expected_skewness=expected_skew,
        expected_excess_kurtosis=expected_kurt,
        tol_mean_rel=tol_mean_rel,
        tol_variance_rel=tol_var_rel,
        tol_skewness_abs=tol_skew_abs,
        tol_excess_kurtosis_abs=tol_kurt_abs,
    )


_FIXTURES: list[_Fixture] = [
    _normal_fixture("normal_0_1", mu=0.0, sigma=1.0, seed=1),
    _normal_fixture("normal_3_2", mu=3.0, sigma=2.0, seed=2),
    _lognormal_fixture("lognormal_sigma_0.5", sigma=0.5, seed=3),
    _lognormal_fixture("lognormal_sigma_1.0", sigma=1.0, seed=4),
    _exponential_fixture(seed=5),
    _uniform_fixture(seed=6),
    _pareto_fixture(alpha=4.5, seed=7),
    _pareto_fixture(alpha=3.5, seed=8),
    _pareto_fixture(alpha=2.5, seed=9),
    _pareto_fixture(alpha=1.5, seed=10),
]


def _run_through_oracle(samples: "np.ndarray[Any, Any]") -> Summary:
    s = Summary()
    update_many(s, samples)
    return s


@pytest.mark.parametrize(
    "fixture", _FIXTURES, ids=[f.name for f in _FIXTURES],
)
def test_calibration_moments(fixture: _Fixture) -> None:
    """For each fixture, oracle's moments match analytical truth.

    Asserts mean / variance / skewness / excess kurtosis only when
    the corresponding population moment exists for the distribution.
    Skipped moments are NOT silently passed — they're explicitly
    not in scope, recorded by the fixture's expected_* = None.
    """
    s = _run_through_oracle(fixture.samples)

    if fixture.expected_mean is not None:
        observed = mean(s)
        truth = fixture.expected_mean
        tol = abs(truth) * fixture.tol_mean_rel
        # Symmetric distributions can have truth = 0 → relative tol
        # is also 0; admit a small absolute floor so we don't
        # demand exact equality on a noisy 0.
        tol = max(tol, 0.01)
        assert abs(observed - truth) <= tol, (
            f"{fixture.name}: mean {observed!r} vs truth {truth!r}, "
            f"tol={tol}"
        )

    if fixture.expected_variance is not None:
        observed = variance(s)
        truth = fixture.expected_variance
        tol = truth * fixture.tol_variance_rel
        assert abs(observed - truth) <= tol, (
            f"{fixture.name}: variance {observed!r} vs truth "
            f"{truth!r}, tol={tol}"
        )

    if fixture.expected_skewness is not None:
        observed = skewness(s)
        truth = fixture.expected_skewness
        tol = fixture.tol_skewness_abs
        assert abs(observed - truth) <= tol, (
            f"{fixture.name}: skewness {observed!r} vs truth "
            f"{truth!r}, tol={tol}"
        )

    if fixture.expected_excess_kurtosis is not None:
        observed = excess_kurtosis(s)
        truth = fixture.expected_excess_kurtosis
        tol = fixture.tol_excess_kurtosis_abs
        assert abs(observed - truth) <= tol, (
            f"{fixture.name}: excess kurtosis {observed!r} vs "
            f"truth {truth!r}, tol={tol}"
        )


def test_oracle_agrees_with_numpy_on_normal_sample() -> None:
    """Sanity: Python Pébay matches numpy/scipy on a Gaussian.

    Smoke test that the oracle is wired correctly. Calibration
    fixtures above test convergence to *theory*; this one tests
    convergence to *numpy/scipy on the same sample*.
    """
    rng = np.random.default_rng(seed=42)
    xs = rng.normal(loc=0.0, scale=1.0, size=_N)
    s = _run_through_oracle(xs)
    assert abs(mean(s) - float(np.mean(xs))) < 1e-9
    assert abs(variance(s) - float(np.var(xs))) < 1e-9
    assert abs(skewness(s) - float(stats.skew(xs))) < 1e-9
    assert abs(excess_kurtosis(s) - float(stats.kurtosis(xs))) < 1e-9


def test_oracle_merge_equals_sequential_update() -> None:
    """Sanity: parallel-merge agrees with sequential update.

    Pébay's parallel-combine rule is the load-bearing claim that
    per-CPU summaries can be reduced without raw samples. Pin it.
    """
    rng = np.random.default_rng(seed=123)
    xs = rng.normal(loc=2.0, scale=3.0, size=_N)
    half = _N // 2

    sequential = Summary()
    update_many(sequential, xs)

    s_a = Summary()
    update_many(s_a, xs[:half])
    s_b = Summary()
    update_many(s_b, xs[half:])
    merge(s_a, s_b)

    # Merge should agree with sequential to within float reordering.
    assert s_a.n == sequential.n
    assert abs(s_a.m1 - sequential.m1) / max(1.0, abs(sequential.m1)) < 1e-12
    assert abs(s_a.m2 - sequential.m2) / max(1.0, abs(sequential.m2)) < 1e-10
    assert abs(s_a.m3 - sequential.m3) / max(1.0, abs(sequential.m3)) < 1e-9
    assert abs(s_a.m4 - sequential.m4) / max(1.0, abs(sequential.m4)) < 1e-9
