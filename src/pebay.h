/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Pébay (2008) online moment update — k=2 partial implementation.
 *
 * Reference: Philippe Pébay, "Formulas for Robust, One-Pass Parallel
 * Computation of Covariances and Arbitrary-Order Statistical Moments,"
 * Sandia National Laboratories SAND2008-6212, 2008.
 *
 * This file is header-only on purpose (D005): the same functions are
 * inlined into the BPF kernel-side program (`src/iomoments.bpf.c`,
 * pending) and the userspace aggregator (`src/iomoments.c`, pending),
 * so there's no call-boundary overhead on the hot path.
 *
 * **Current scope — Welford (k=2 case) only.** Pébay's update at
 * arbitrary order generalizes Welford (1962) for mean + variance with
 * a parallel-combine rule that merges two partial summaries into the
 * summary of their union. This file implements k=2 — mean and variance
 * plus merge — because that's the textbook-verifiable subset. Higher
 * moments (M3, M4) land in a follow-up commit once this foundation is
 * gate-proven.
 *
 * Numerical stability rationale (D006):
 * - Sum-of-powers accumulation (tracking Σxⁱ directly) is rejected —
 *   catastrophic cancellation makes higher-order sample moments
 *   unusable on realistic I/O streams. Pébay's central-moment form
 *   avoids this.
 * - Welford's delta-based update subtracts the running mean from each
 *   sample, so the intermediate quantities stay bounded by the sample
 *   spread rather than growing with the sample count.
 */

#ifndef IOMOMENTS_PEBAY_H
#define IOMOMENTS_PEBAY_H

#include <stddef.h>
#include <stdint.h>

/*
 * IOMOMENTS_INLINE controls whether the update/merge functions end up
 * as real call boundaries or inlined at every use site. Under clang's
 * BPF target the verifier rejects function calls into helpers not
 * marked always_inline, so we force it there; in userspace -O2 the
 * static inline hint is enough for gcc/clang to inline the tiny
 * bodies without pinning it.
 */
#ifndef IOMOMENTS_INLINE
#if defined(__bpf__)
#define IOMOMENTS_INLINE static inline __attribute__((always_inline))
#else
#define IOMOMENTS_INLINE static inline
#endif
#endif

/*
 * Running moment summary for a single stream of samples.
 *
 * Field naming follows Pébay 2008 notation:
 *   n   - count of samples processed so far.
 *   m1  - running mean.
 *   m2  - running sum of squared deviations from the mean
 *         (NOT variance — variance is m2 / n, and that division is
 *         deferred until read-out so the summary is mergeable by
 *         Pébay's parallel-combine rule without losing precision).
 *
 * Fits comfortably in one cache line (24 bytes with 8-byte alignment)
 * per the D009 per_cpu_update_bytes performance constraint (budget:
 * 64 bytes). Stays under budget at k=2; M3/M4 will push it higher.
 */
struct iomoments_summary {
	uint64_t n;
	double m1;
	double m2;
};

/*
 * Zero-initialize a summary. Usable as a static initializer
 * (`struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;`) or via the
 * helper below for runtime paths.
 */
#define IOMOMENTS_SUMMARY_ZERO                                                 \
	{                                                                      \
		0, 0.0, 0.0                                                    \
	}

IOMOMENTS_INLINE void iomoments_summary_init(struct iomoments_summary *s)
{
	s->n = 0;
	s->m1 = 0.0;
	s->m2 = 0.0;
}

/*
 * Incorporate a single new sample x into the running summary s.
 *
 * Algorithm: Welford (1962) — equivalent to Pébay 2008 eq. (2.1),
 * (2.2) at k=2. One branch-free update per sample; intermediate
 * quantities stay bounded by the sample spread.
 *
 *   n'      = n + 1
 *   δ       = x - m1
 *   δ/n'    — running-mean adjustment
 *   m1     += δ/n'
 *   m2     += δ · (x - m1_new)      [i.e. δ · (δ - δ/n') = δ²(n/n')]
 *
 * The second form of the m2 update uses the NEW m1 (after the
 * increment). That's what makes it stable: (x - m1_new) is the
 * residual against the updated mean, not the pre-update one.
 */
/*
 * Caller is responsible for preventing uint64_t overflow of `n`. At
 * 2^64 samples that's ~584 years of 1-ns-per-sample updates; not a
 * realistic concern for iomoments' workload sizes, so the cost of a
 * saturation check every update is refused here.
 */
IOMOMENTS_INLINE void iomoments_summary_update(struct iomoments_summary *s,
					       double x)
{
	s->n += 1;
	double delta = x - s->m1;
	s->m1 += delta / (double)s->n;
	double delta_post = x - s->m1;
	s->m2 += delta * delta_post;
}

/*
 * Merge partial summary `b` into `a` in place.
 *
 * Algorithm: Pébay 2008 eq. (3.2), (3.3) at k=2. This is the rule
 * that makes per-CPU accumulation work — each CPU runs its own
 * summary; userspace merges them to a single aggregate without
 * re-scanning the raw samples.
 *
 *   n       = n_a + n_b
 *   δ       = m1_b - m1_a
 *   m1_new  = (n_a · m1_a + n_b · m1_b) / n
 *   m2_new  = m2_a + m2_b + δ² · n_a · n_b / n
 *
 * The δ² · n_a · n_b / n term is the "correction" for the variance
 * estimate needing to account for the gap between the two
 * sub-streams' means. Goes to zero when m1_a = m1_b, as expected.
 *
 * Merging into an empty `a` (n_a = 0) is the identity on `b`. We
 * handle that explicitly to skip the division that would otherwise
 * compute 0/0.
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
	/*
	 * Snapshot `b` first so the function is aliasing-safe: if a
	 * caller writes iomoments_summary_merge(&s, &s) (semantically
	 * well-defined: observing a stream twice doubles n and m2,
	 * keeps m1), the writes to `a` below would otherwise clobber
	 * the fields we still need to read out of `b`. The optimizer
	 * elides the snapshot when the compiler can prove a != b.
	 */
	struct iomoments_summary b_snap = *b;
	uint64_t n = a->n + b_snap.n;
	double delta = b_snap.m1 - a->m1;
	double m1_new = (double)a->n / (double)n * a->m1 +
			(double)b_snap.n / (double)n * b_snap.m1;
	double correction =
		delta * delta * (double)a->n * (double)b_snap.n / (double)n;
	a->n = n;
	a->m1 = m1_new;
	a->m2 = a->m2 + b_snap.m2 + correction;
}

/*
 * Return the mean. Stable contract: a zero-initialized summary returns
 * 0.0 (the m1 field is zero-initialized and no updates have occurred).
 * Callers that need to distinguish "no samples yet" from "mean is 0"
 * must check n == 0 explicitly before interpreting the value.
 */
IOMOMENTS_INLINE double
iomoments_summary_mean(const struct iomoments_summary *s)
{
	return s->m1;
}

/*
 * Return the population variance (σ² = m2 / n). Undefined (returns
 * 0.0) when n == 0.
 *
 * Population vs sample variance: this function returns the population
 * form because iomoments summarizes the observed workload, not
 * inferences about a larger population from a sample. If a downstream
 * consumer needs the sample variance (unbiased estimator, n-1
 * divisor), they can recompute it from m2 and n directly.
 */
IOMOMENTS_INLINE double
iomoments_summary_variance(const struct iomoments_summary *s)
{
	if (s->n == 0) {
		return 0.0;
	}
	return s->m2 / (double)s->n;
}

#endif /* IOMOMENTS_PEBAY_H */
