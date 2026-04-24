/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Pébay (2008) online moment update at k=4.
 *
 * Reference: Philippe Pébay, "Formulas for Robust, One-Pass Parallel
 * Computation of Covariances and Arbitrary-Order Statistical Moments,"
 * Sandia National Laboratories SAND2008-6212, 2008.
 *
 * Userspace canonical (double-precision) companion to src/pebay_bpf.h.
 * Tracks M1 (running mean), M2 (sum of squared deviations), M3 and M4
 * (sum of cubed and fourth-power deviations) via Pébay's
 * numerically-stable update + parallel-combine rules. Readouts:
 *
 *   mean     = M1
 *   variance = M2 / n
 *   skewness = sqrt(n) · M3 / M2^(3/2)
 *   kurtosis = n · M4 / M2² - 3    (excess kurtosis; 0 for Gaussian)
 *
 * Numerical stability rationale (D006):
 * - Sum-of-powers accumulation (tracking Σxᵏ directly) is rejected —
 *   catastrophic cancellation makes higher-order sample moments
 *   unusable on realistic I/O streams. Pébay's central-moment form
 *   avoids this.
 * - Welford's delta-based update subtracts the running mean from each
 *   sample, so the intermediate quantities stay bounded by the sample
 *   spread rather than growing with the sample count.
 */

#ifndef IOMOMENTS_PEBAY_H
#define IOMOMENTS_PEBAY_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>

/*
 * IOMOMENTS_INLINE controls whether the update/merge functions end up
 * as real call boundaries or inlined at every use site.
 */
#ifndef IOMOMENTS_INLINE
#define IOMOMENTS_INLINE static inline
#endif

/*
 * Running moment summary for a single stream of samples.
 *
 * Field naming follows Pébay 2008 notation:
 *   n      - count of samples processed so far.
 *   m1     - running mean.
 *   m2..m4 - running sums of (x - m1)^k for k = 2, 3, 4. Variance,
 *            skewness, and excess kurtosis are derived from these +
 *            n at read-out time.
 *
 * 40 bytes with 8-byte alignment. This is the userspace-only shape
 * (per D011, the BPF kernel-side path uses pebay_bpf.h with its own
 * fixed-point storage layout).
 */
struct iomoments_summary {
	uint64_t n;
	double m1;
	double m2;
	double m3;
	double m4;
};

#define IOMOMENTS_SUMMARY_ZERO                                                 \
	{                                                                      \
		0, 0.0, 0.0, 0.0, 0.0                                          \
	}

IOMOMENTS_INLINE void iomoments_summary_init(struct iomoments_summary *s)
{
	s->n = 0;
	s->m1 = 0.0;
	s->m2 = 0.0;
	s->m3 = 0.0;
	s->m4 = 0.0;
}

/*
 * Incorporate one sample x into the running summary.
 *
 * Algorithm: Pébay 2008 eq. (2.1) at k=4. Updated field order is
 * load-bearing — M4 uses the OLD M2 and M3, then M3 uses the OLD M2,
 * then M2 updates last. Do NOT reorder without recomputing the
 * algebraic equivalence.
 *
 *   n_old    = n
 *   n       += 1
 *   δ        = x - m1                      (pre-update residual)
 *   δ/n      — mean adjustment
 *   δ/n²     = (δ/n)² — convenience
 *   term1    = δ · (δ/n) · n_old           = δ² · n_old / n
 *   m1      += δ/n
 *   m4      += term1 · (δ/n)² · (n² - 3n + 3)
 *              + 6 (δ/n)² m2 - 4 (δ/n) m3
 *   m3      += term1 · (δ/n) · (n - 2) - 3 (δ/n) m2
 *   m2      += term1
 */
IOMOMENTS_INLINE void iomoments_summary_update(struct iomoments_summary *s,
					       double x)
{
	uint64_t n_old = s->n;
	s->n += 1;
	double nf = (double)s->n;
	double delta = x - s->m1;
	double delta_n = delta / nf;
	double delta_n2 = delta_n * delta_n;
	double term1 = delta * delta_n * (double)n_old;
	s->m1 += delta_n;
	s->m4 += term1 * delta_n2 * (nf * nf - 3.0 * nf + 3.0) +
		 6.0 * delta_n2 * s->m2 - 4.0 * delta_n * s->m3;
	s->m3 += term1 * delta_n * (nf - 2.0) - 3.0 * delta_n * s->m2;
	s->m2 += term1;
}

/*
 * Merge partial summary `b` into `a` in place.
 *
 * Algorithm: Pébay 2008 eq. (3.2)-(3.5) at k=4. Parallel-combine rule
 * that lets per-CPU accumulators be reduced to a single aggregate
 * without replaying raw samples.
 *
 *   n       = n_a + n_b
 *   δ       = m1_b - m1_a
 *   m1      = (n_a · m1_a + n_b · m1_b) / n
 *   m2      = m2_a + m2_b + δ² · n_a · n_b / n
 *   m3      = m3_a + m3_b
 *           + δ³ · n_a · n_b · (n_a - n_b) / n²
 *           + 3 δ · (n_a · m2_b - n_b · m2_a) / n
 *   m4      = m4_a + m4_b
 *           + δ⁴ · n_a · n_b · (n_a² - n_a·n_b + n_b²) / n³
 *           + 6 δ² · (n_a² · m2_b + n_b² · m2_a) / n²
 *           + 4 δ · (n_a · m3_b - n_b · m3_a) / n
 *
 * Aliasing-safe: `b` is snapshotted before any write to `a`, so
 * iomoments_summary_merge(&s, &s) is a well-defined self-merge.
 */
IOMOMENTS_INLINE void iomoments_summary_merge(struct iomoments_summary *a,
					      const struct iomoments_summary *b)
{
	if (b->n == 0) {
		return;
	}
	if (a->n == 0) {
		*a = *b;
		return;
	}
	struct iomoments_summary b_snap = *b;
	uint64_t n = a->n + b_snap.n;
	double nf = (double)n;
	double n_a = (double)a->n;
	double n_b = (double)b_snap.n;
	double delta = b_snap.m1 - a->m1;
	double delta2 = delta * delta;
	double delta3 = delta2 * delta;
	double delta4 = delta3 * delta;

	double m1_new = n_a / nf * a->m1 + n_b / nf * b_snap.m1;

	double m2_correction = delta2 * n_a * n_b / nf;
	double m2_new = a->m2 + b_snap.m2 + m2_correction;

	double m3_correction =
		delta3 * n_a * n_b * (n_a - n_b) / (nf * nf) +
		3.0 * delta * (n_a * b_snap.m2 - n_b * a->m2) / nf;
	double m3_new = a->m3 + b_snap.m3 + m3_correction;

	double m4_correction =
		delta4 * n_a * n_b * (n_a * n_a - n_a * n_b + n_b * n_b) /
			(nf * nf * nf) +
		6.0 * delta2 * (n_a * n_a * b_snap.m2 + n_b * n_b * a->m2) /
			(nf * nf) +
		4.0 * delta * (n_a * b_snap.m3 - n_b * a->m3) / nf;
	double m4_new = a->m4 + b_snap.m4 + m4_correction;

	a->n = n;
	a->m1 = m1_new;
	a->m2 = m2_new;
	a->m3 = m3_new;
	a->m4 = m4_new;
}

/* --- Readouts --------------------------------------------------------- */

IOMOMENTS_INLINE double
iomoments_summary_mean(const struct iomoments_summary *s)
{
	return s->m1;
}

/*
 * Population variance (σ² = m2 / n). Zero on empty summary.
 */
IOMOMENTS_INLINE double
iomoments_summary_variance(const struct iomoments_summary *s)
{
	if (s->n == 0) {
		return 0.0;
	}
	return s->m2 / (double)s->n;
}

/*
 * Population skewness γ₁ = E[(X-μ)³] / σ³
 *                       = (M3/n) / (M2/n)^(3/2)
 *                       = √n · M3 / M2^(3/2)
 *
 * Returns 0.0 on empty summary or when M2 is near-zero (constant
 * stream — skewness undefined). Caller should check n > some
 * threshold and M2 > some threshold for meaningful interpretation;
 * iomoments' diagnostic layer (D007) surfaces the "too few samples /
 * distribution too narrow" cases as separate signals.
 */
IOMOMENTS_INLINE double
iomoments_summary_skewness(const struct iomoments_summary *s)
{
	if (s->n == 0 || s->m2 <= 0.0) {
		return 0.0;
	}
	/* √n · M3 / M2^(3/2) */
	double nf = (double)s->n;
	double m2_pow_1_5 = s->m2 * sqrt(s->m2);
	return sqrt(nf) * s->m3 / m2_pow_1_5;
}

/*
 * Population excess kurtosis γ₂ = E[(X-μ)⁴] / σ⁴ - 3
 *                              = n · M4 / M2² - 3
 *
 * Zero for Gaussian. Positive for heavy-tailed / peaked
 * distributions. Returns 0.0 on empty summary or near-zero M2.
 */
IOMOMENTS_INLINE double
iomoments_summary_excess_kurtosis(const struct iomoments_summary *s)
{
	if (s->n == 0 || s->m2 <= 0.0) {
		return 0.0;
	}
	double nf = (double)s->n;
	return nf * s->m4 / (s->m2 * s->m2) - 3.0;
}

#endif /* IOMOMENTS_PEBAY_H */
