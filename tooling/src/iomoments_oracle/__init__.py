"""Python numerical oracle for iomoments' Pébay update rules.

A line-for-line port of ``src/pebay.h`` (Pébay 2008 SAND2008-6212,
k=4) into pure Python with ``float`` (64-bit IEEE 754) arithmetic.
The userspace C path uses ``double``, so a faithful port is exact-
agreeing modulo floating-point reordering — which the tests pin
against scipy + numpy on canonical distributions.

Why a Python reference exists alongside the C implementation:

1. The C tests in ``tests/c/test_pebay.c`` validate the C update
   rules against a small fixed-point fixture. They do not validate
   the algorithm itself against analytical truth on canonical
   distributions; that's what the calibration tests under
   ``tests/test_calibration_moments.py`` cover, and they need a
   Python SUT.
2. Having a Python implementation lets calibration tests use
   ``scipy.stats.<dist>.stats(moments='mvsk')`` for ground truth
   without a subprocess hop into a C harness.
3. If the C implementation drifts in a way the C tests don't catch,
   the Python oracle becomes the second witness in a future
   "compare C-side moments against Python-side moments on the same
   sample" producer. (Not built today; tracked in MEMORY.)

Variable naming mirrors ``src/pebay.h`` exactly (``m1..m4``,
``delta``, ``delta_n``, ``term1``) so a side-by-side read is
straightforward.

D006 / D007 readout conventions:
- mean ``μ`` returned as-is from m1.
- variance is the **population** variance σ² = m2 / n, matching
  ``iomoments_summary_variance`` in pebay.h.
- skewness is Fisher's γ₁ = √n · m3 / m2^(3/2); matches
  ``iomoments_summary_skewness``.
- excess kurtosis is γ₂ = n · m4 / m2² - 3; matches
  ``iomoments_summary_excess_kurtosis``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import sqrt


@dataclass
class Summary:
    """Running k=4 Pébay summary in float64.

    Mirrors ``struct iomoments_summary`` in src/pebay.h.
    """

    n: int = 0
    m1: float = 0.0
    m2: float = 0.0
    m3: float = 0.0
    m4: float = 0.0


def update(s: Summary, x: float) -> None:
    """Pébay 2008 single-sample update at k=4. In-place.

    Direct port of ``iomoments_summary_update``. The order of the
    m4 / m3 / m2 / m1 assignments matters — m4 reads the m2 and m3
    BEFORE they're updated this step, m3 reads m2 BEFORE the
    update, m2 is updated last. Reordering is a correctness bug.
    """
    n_old = s.n
    s.n += 1
    nf = float(s.n)
    delta = x - s.m1
    delta_n = delta / nf
    delta_n2 = delta_n * delta_n
    term1 = delta * delta_n * float(n_old)
    s.m1 += delta_n
    s.m4 += (
        term1 * delta_n2 * (nf * nf - 3.0 * nf + 3.0)
        + 6.0 * delta_n2 * s.m2
        - 4.0 * delta_n * s.m3
    )
    s.m3 += term1 * delta_n * (nf - 2.0) - 3.0 * delta_n * s.m2
    s.m2 += term1


def merge(a: Summary, b: Summary) -> None:
    """Parallel-combine merge: fold ``b`` into ``a`` in place.

    Direct port of ``iomoments_summary_merge``. Aliasing-safe:
    snapshots ``b`` before any write to ``a`` so ``merge(s, s)``
    is a well-defined self-merge (n doubles, moments stay correct).
    """
    if b.n == 0:
        return
    if a.n == 0:
        a.n, a.m1, a.m2, a.m3, a.m4 = b.n, b.m1, b.m2, b.m3, b.m4
        return
    b_n, b_m1, b_m2, b_m3, b_m4 = b.n, b.m1, b.m2, b.m3, b.m4
    n = a.n + b_n
    nf = float(n)
    n_a = float(a.n)
    n_b = float(b_n)
    delta = b_m1 - a.m1
    delta2 = delta * delta
    delta3 = delta2 * delta
    delta4 = delta3 * delta

    m1_new = n_a / nf * a.m1 + n_b / nf * b_m1
    m2_correction = delta2 * n_a * n_b / nf
    m2_new = a.m2 + b_m2 + m2_correction
    m3_correction = (
        delta3 * n_a * n_b * (n_a - n_b) / (nf * nf)
        + 3.0 * delta * (n_a * b_m2 - n_b * a.m2) / nf
    )
    m3_new = a.m3 + b_m3 + m3_correction
    m4_correction = (
        delta4 * n_a * n_b * (n_a * n_a - n_a * n_b + n_b * n_b)
        / (nf * nf * nf)
        + 6.0 * delta2 * (n_a * n_a * b_m2 + n_b * n_b * a.m2)
        / (nf * nf)
        + 4.0 * delta * (n_a * b_m3 - n_b * a.m3) / nf
    )
    m4_new = a.m4 + b_m4 + m4_correction

    a.n = n
    a.m1 = m1_new
    a.m2 = m2_new
    a.m3 = m3_new
    a.m4 = m4_new


def mean(s: Summary) -> float:
    """Sample mean. Matches iomoments_summary_mean."""
    return s.m1


def variance(s: Summary) -> float:
    """Population variance σ² = m2 / n. Zero on empty.

    Matches iomoments_summary_variance — population, not sample
    (no Bessel correction). The C path's downstream consumers
    (variance_sanity, kurtosis_sanity, JB) are all built on the
    population convention.
    """
    if s.n == 0:
        return 0.0
    return s.m2 / float(s.n)


def skewness(s: Summary) -> float:
    """Fisher's γ₁ = √n · m3 / m2^(3/2). Zero on empty / m2 ≤ 0.

    Matches iomoments_summary_skewness. The "empty / m2 ≤ 0" guard
    is structural — skewness is undefined on a constant stream and
    iomoments' diagnostic layer surfaces those as separate signals.
    """
    if s.n == 0 or s.m2 <= 0.0:
        return 0.0
    nf = float(s.n)
    m2_pow_1_5 = s.m2 * sqrt(s.m2)
    return sqrt(nf) * s.m3 / m2_pow_1_5


def excess_kurtosis(s: Summary) -> float:
    """γ₂ = n · m4 / m2² - 3. Zero for Gaussian.

    Matches iomoments_summary_excess_kurtosis. Same constant-stream
    guard as skewness.
    """
    if s.n == 0 or s.m2 <= 0.0:
        return 0.0
    nf = float(s.n)
    return nf * s.m4 / (s.m2 * s.m2) - 3.0


def update_many(s: Summary, xs: Iterable[float]) -> None:
    """Apply ``update`` to each sample in ``xs`` in order.

    Convenience for tests; ``xs`` is anything iterable yielding
    floats (numpy arrays of floats qualify). Equivalent to
    ``for x in xs: update(s, float(x))`` but written here so
    callers don't repeat the float coercion.
    """
    for x in xs:
        update(s, float(x))


__all__ = [
    "Summary",
    "excess_kurtosis",
    "mean",
    "merge",
    "skewness",
    "update",
    "update_many",
    "variance",
]
