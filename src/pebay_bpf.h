/* SPDX-License-Identifier: GPL-2.0-only OR AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Pébay (2008) online moment update — BPF-safe fixed-point variant
 * at k=4 (mean, variance, skewness, excess kurtosis). Companion to
 * src/pebay.h, which uses double.
 *
 * Why two headers (D011): the BPF verifier rejects floating-point
 * instructions in tracing attach points; the kernel does not
 * save/restore FP registers per-invocation for those contexts. So
 * iomoments has:
 *
 *   - src/pebay.h         double, userspace + testing oracle.
 *   - src/pebay_bpf.h     fixed-point, kernel-side BPF hot path.
 *
 * A round-trip property test (tests/c/test_pebay_bpf.c) pins the
 * two implementations to a stated agreement tolerance on integer-
 * valued input streams. The userspace reference is canonical;
 * pebay_bpf.h is an approximation pinned to not drift off it.
 *
 * -----------------------------------------------------------------
 * Storage (k=4):
 * -----------------------------------------------------------------
 *
 *   m1_fp  iomoments_s64 in Q32.32 fixed-point ns. Integer part up
 *          to 2^31 ns (~2.1 s); fractional 2^-32 ns precision.
 *   m2     iomoments_s64 in raw ns² (Q0.0). Sum of squared deviations.
 *   m3     struct s128 in raw ns³.  Sum of cubed deviations.
 *   m4     struct s128 in raw ns⁴.  Sum of fourth-power deviations.
 *
 * Total footprint: 8 (n) + 8 (m1_fp) + 8 (m2) + 16 (m3) + 16 (m4)
 * = 56 bytes per CPU. Within D009's 64-byte per_cpu_update_bytes
 * budget.
 *
 * Why s128 for m3, m4: realistic workloads (n=10⁹ samples at
 * σ=10μs) drive m3 ≈ n·γ·σ³ ≈ 10²¹ and m4 ≈ n·σ⁴ ≈ 10²⁵, both well
 * over s64's 9.2·10¹⁸ ceiling. Per-sample products in the update
 * also exceed s64 transiently. u128.h provides the multi-precision
 * primitives (s128_add/sub, s64×s64→s128, s128×u64, s128×s64,
 * s128÷u64 via Knuth D); the BPF verifier accepts hand-rolled
 * 64-bit ops but rejects compiler __multi3/__divti3 libcalls.
 *
 * Why m2 stays s64: n·σ² stays under s64 max for σ ≤ 1ms at
 * n ≤ 9·10⁹, comfortably above realistic per-CPU summary lifetimes.
 * Documented limitation; userspace must drain summaries before
 * pathological long runs at high σ.
 *
 * -----------------------------------------------------------------
 * Precision vs pebay.h
 * -----------------------------------------------------------------
 *
 * For integer-ns inputs:
 *   - mean: Q32.32 / N agrees with pebay.h to ~1 ULP at low N,
 *     converging to bit-exact for many samples.
 *   - variance: integer-ns truncation in the Welford update
 *     accumulates ≤ 1 ns² per sample. Negligible at σ >> 1 ns.
 *   - skewness, excess kurtosis: each delta-product step truncates
 *     to integer ns, so M3 and M4 lose sub-ns fractional
 *     contributions per update. Relative error scales as
 *     (1/σ)^k for the kth moment. At μs-scale σ the error is
 *     ~1e-6 relative; at ns-scale σ (pathological textbook
 *     fixtures) it's perceptible (~10% on M4).
 *
 * The round-trip test pins this with documented tolerances per
 * fixture.
 *
 * -----------------------------------------------------------------
 * Merge:
 * -----------------------------------------------------------------
 *
 * iomoments_summary_bpf_merge implements Pébay eq (3.2)-(3.5) at
 * k=4 using the same multi-precision primitives. It is NOT called
 * on the BPF hot path — production aggregation goes BPF →
 * bpf_summary_to_ref → pebay.h merge in double. The BPF-side merge
 * exists so that the round-trip test can verify the fixed-point
 * parallel-combine rule against the canonical double-precision
 * one.
 */

#ifndef IOMOMENTS_PEBAY_BPF_H
#define IOMOMENTS_PEBAY_BPF_H

#include "u128.h"

#ifndef IOMOMENTS_BPF_INLINE
#if defined(__bpf__)
#define IOMOMENTS_BPF_INLINE static inline __attribute__((always_inline))
#else
#define IOMOMENTS_BPF_INLINE static inline
#endif
#endif

/*
 * BPF verifier: under __bpf__ the always_inline hint is required or
 * the verifier rejects helper-function calls into non-inlined code.
 * Under regular userspace compile (test driver), plain static inline
 * is enough; gcc/clang inline at -O2 anyway.
 */

#define IOMOMENTS_BPF_FRAC_BITS 32

struct iomoments_summary_bpf {
	iomoments_u64 n;
	iomoments_s64 m1_fp; /* Q32.32 signed ns */
	iomoments_s64 m2; /* raw ns² — integer-approximate Welford accumulator */
	struct s128 m3; /* raw ns³ */
	struct s128 m4; /* raw ns⁴ */
};

#define IOMOMENTS_SUMMARY_BPF_ZERO                                             \
	{                                                                      \
		0, 0, 0, {0, 0},                                               \
		{                                                              \
			0, 0                                                   \
		}                                                              \
	}

IOMOMENTS_BPF_INLINE void
iomoments_summary_bpf_init(struct iomoments_summary_bpf *s)
{
	s->n = 0;
	s->m1_fp = 0;
	s->m2 = 0;
	s->m3 = s128_zero();
	s->m4 = s128_zero();
}

/*
 * The BPF target rejects signed division; do unsigned division on
 * the absolute value and restore the sign manually. n is always > 0
 * when this helper is called (update incremented it above).
 */
IOMOMENTS_BPF_INLINE iomoments_s64 iomoments_bpf_signed_div(iomoments_s64 num,
							    iomoments_u64 den)
{
	if (num >= 0) {
		return (iomoments_s64)((iomoments_u64)num / den);
	}
	return -(iomoments_s64)((iomoments_u64)(-num) / den);
}

/*
 * Incorporate one sample x (nanoseconds integer) into the summary.
 *
 * Pébay 2008 eq (2.1) at k=4, integer-ns form. Update order is
 * load-bearing — m4 uses the OLD m2 and m3, m3 uses the OLD m2,
 * m2 updates last. Do NOT reorder without recomputing the
 * algebraic equivalence.
 *
 *   n_old = n; n += 1
 *   δ_fp     = (x << 32) - m1_fp                         (Q32.32 ns)
 *   m1_fp   += δ_fp / n
 *   δ_int    = δ_fp >> 32                                (s64 ns)
 *   δ²       = δ_int · δ_int                             (s64, ≥0)
 *   m4 += δ⁴·n_old·(n²-3n+3)/n³ + 6·δ²·m2/n² - 4·δ·m3/n
 *   m3 += δ³·n_old·(n-2)/n²    - 3·δ·m2/n
 *   m2 += δ_int · δ_post_int          (existing Welford k=2)
 *
 * Each multi-precision term is computed with multiplies and
 * divides interleaved to keep s128 intermediates within bounds at
 * worst-case δ saturation (δ ≤ 2³¹).
 */
IOMOMENTS_BPF_INLINE void
iomoments_summary_bpf_update(struct iomoments_summary_bpf *s, iomoments_u64 x)
{
	iomoments_u64 n_old = s->n;
	s->n += 1;
	iomoments_u64 n = s->n;

	iomoments_s64 x_fp = (iomoments_s64)(x << IOMOMENTS_BPF_FRAC_BITS);
	iomoments_s64 delta_fp = x_fp - s->m1_fp;
	iomoments_s64 delta_n_fp = iomoments_bpf_signed_div(delta_fp, n);
	s->m1_fp += delta_n_fp;
	iomoments_s64 delta_post_fp = x_fp - s->m1_fp;

	iomoments_s64 delta_int = delta_fp >> IOMOMENTS_BPF_FRAC_BITS;
	iomoments_s64 delta_post_int = delta_post_fp >> IOMOMENTS_BPF_FRAC_BITS;
	iomoments_s64 delta_sq = delta_int * delta_int; /* ≥ 0, fits s64 */

	/* Snapshot OLD m2, m3 — m4 update reads both, m3 update reads m2. */
	iomoments_s64 m2_old = s->m2;
	struct s128 m3_old = s->m3;

	/* === m4 update ============================================ */
	struct s128 m4_inc = s128_zero();

	/* Term 1: δ⁴·n_old·(n²-3n+3)/n³.
	 * For n=1 (n_old=0) the term is identically zero — skip. */
	if (n_old > 0) {
		/* polynomial = n²-3n+3. n=2: 1, n=3: 3, n=4: 7, all ≥1. */
		iomoments_u64 polynomial = n * n - 3 * n + 3;
		struct s128 t = s64_mul_s64(delta_sq, delta_sq); /* δ⁴ */
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, n_old);
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, polynomial);
		t = s128_div_u64(t, n);
		m4_inc = s128_add(m4_inc, t);
	}

	/* Term 2: 6·δ²·m2/n². Divide first to keep s128 bounded. */
	{
		struct s128 t = s64_mul_s64(delta_sq, m2_old);
		t = s128_div_u64(t, n);
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, 6);
		m4_inc = s128_add(m4_inc, t);
	}

	/* Term 3: -4·δ·m3/n. */
	{
		struct s128 t = s128_mul_s64(m3_old, delta_int);
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, 4);
		m4_inc = s128_sub(m4_inc, t);
	}

	s->m4 = s128_add(s->m4, m4_inc);

	/* === m3 update (uses OLD m2) ============================== */
	struct s128 m3_inc = s128_zero();

	/* Term 1: δ³·n_old·(n-2)/n². For n < 3 the (n-2)·n_old factor is
	 * 0; skip to avoid u64 underflow on (n-2). */
	if (n >= 3) {
		struct s128 delta_cube = s64_mul_s64(delta_sq, delta_int);
		struct s128 t = s128_mul_u64(delta_cube, n_old);
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, n - 2);
		t = s128_div_u64(t, n);
		m3_inc = s128_add(m3_inc, t);
	}

	/* Term 2: -3·δ·m2/n. */
	{
		struct s128 t = s64_mul_s64(delta_int, m2_old);
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, 3);
		m3_inc = s128_sub(m3_inc, t);
	}

	s->m3 = s128_add(s->m3, m3_inc);

	/* === m2 update (Welford k=2, unchanged from prior) ======== */
	s->m2 += delta_int * delta_post_int;
}

/*
 * Merge partial summary `b` into `a` in place. Pébay 2008
 * eq (3.2)-(3.5) at k=4, integer-ns form.
 *
 *   n      = n_a + n_b
 *   δ      = m1_b - m1_a
 *   m1     = (n_a·m1_a + n_b·m1_b) / n
 *   m2    += δ²·n_a·n_b / n
 *   m3    += δ³·n_a·n_b·(n_a-n_b)/n² + 3δ·(n_a·m2_b - n_b·m2_a)/n
 *   m4    += δ⁴·n_a·n_b·(n_a²-n_a·n_b+n_b²)/n³
 *           + 6δ²·(n_a²·m2_b + n_b²·m2_a)/n²
 *           + 4δ·(n_a·m3_b - n_b·m3_a)/n
 *
 * Aliasing-safe: snapshot `b` before any write to `a`, matching
 * pebay.h's convention so iomoments_summary_bpf_merge(&s, &s) is
 * well-defined.
 *
 * NOT on the BPF hot path. iomoments.c reads each per-CPU summary
 * via bpf_summary_to_ref and merges in double via pebay.h. This
 * BPF-side merge exists for round-trip testing of the integer
 * parallel-combine rule against the canonical one.
 */
IOMOMENTS_BPF_INLINE void
iomoments_summary_bpf_merge(struct iomoments_summary_bpf *a,
			    const struct iomoments_summary_bpf *b)
{
	if (b->n == 0) {
		return;
	}
	if (a->n == 0) {
		*a = *b;
		return;
	}
	struct iomoments_summary_bpf b_snap = *b;
	iomoments_u64 n = a->n + b_snap.n;
	iomoments_u64 n_a = a->n;
	iomoments_u64 n_b = b_snap.n;

	iomoments_s64 delta_fp = b_snap.m1_fp - a->m1_fp;
	iomoments_s64 delta_int = delta_fp >> IOMOMENTS_BPF_FRAC_BITS;
	iomoments_s64 delta_sq = delta_int * delta_int; /* ≥ 0 */

	/* m1 (Welford k=2 — unchanged from prior). */
	iomoments_s64 m1_a_int = a->m1_fp >> IOMOMENTS_BPF_FRAC_BITS;
	iomoments_s64 m1_b_int = b_snap.m1_fp >> IOMOMENTS_BPF_FRAC_BITS;
	iomoments_s64 m1_new_int_num =
		m1_a_int * (iomoments_s64)n_a + m1_b_int * (iomoments_s64)n_b;
	iomoments_s64 m1_new_int = iomoments_bpf_signed_div(m1_new_int_num, n);
	iomoments_s64 m1_new_fp = m1_new_int << IOMOMENTS_BPF_FRAC_BITS;

	/* m2 correction: δ²·n_a·n_b/n. */
	iomoments_u64 n_product = n_a * n_b;
	iomoments_s64 m2_correction = (iomoments_s64)(n_product / n) * delta_sq;
	iomoments_s64 m2_new = a->m2 + b_snap.m2 + m2_correction;

	/* m3 corrections: δ³·n_a·n_b·(n_a-n_b)/n² + 3δ·(n_a·m2_b - n_b·m2_a)/n.
	 */
	struct s128 m3_correction = s128_zero();
	{
		struct s128 delta_cube = s64_mul_s64(delta_sq, delta_int);
		iomoments_s64 n_diff = (iomoments_s64)n_a - (iomoments_s64)n_b;
		struct s128 t = s128_mul_u64(delta_cube, n_a);
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, n_b);
		t = s128_div_u64(t, n);
		t = s128_mul_s64(t, n_diff);
		m3_correction = s128_add(m3_correction, t);
	}
	{
		struct s128 prod_a = s64_mul_s64((iomoments_s64)n_a, b_snap.m2);
		struct s128 prod_b = s64_mul_s64((iomoments_s64)n_b, a->m2);
		struct s128 diff = s128_sub(prod_a, prod_b);
		diff = s128_mul_s64(diff, delta_int);
		diff = s128_div_u64(diff, n);
		diff = s128_mul_u64(diff, 3);
		m3_correction = s128_add(m3_correction, diff);
	}
	struct s128 m3_new = s128_add(a->m3, b_snap.m3);
	m3_new = s128_add(m3_new, m3_correction);

	/* m4 corrections. */
	struct s128 m4_correction = s128_zero();
	/* Term: δ⁴·n_a·n_b·(n_a²-n_a·n_b+n_b²)/n³. */
	{
		iomoments_u64 polynomial = n_a * n_a - n_a * n_b + n_b * n_b;
		struct s128 delta_4 = s64_mul_s64(delta_sq, delta_sq);
		struct s128 t = s128_div_u64(delta_4, n);
		t = s128_mul_u64(t, n_a);
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, n_b);
		t = s128_div_u64(t, n);
		t = s128_mul_u64(t, polynomial);
		m4_correction = s128_add(m4_correction, t);
	}
	/* Term: 6δ²·(n_a²·m2_b + n_b²·m2_a)/n². */
	{
		iomoments_u64 n_a_sq = n_a * n_a;
		iomoments_u64 n_b_sq = n_b * n_b;
		struct s128 prod_a =
			s128_mul_u64(s128_from_s64(b_snap.m2), n_a_sq);
		struct s128 prod_b = s128_mul_u64(s128_from_s64(a->m2), n_b_sq);
		struct s128 sum = s128_add(prod_a, prod_b);
		sum = s128_mul_u64(sum, (iomoments_u64)delta_sq);
		sum = s128_div_u64(sum, n);
		sum = s128_div_u64(sum, n);
		sum = s128_mul_u64(sum, 6);
		m4_correction = s128_add(m4_correction, sum);
	}
	/* Term: 4δ·(n_a·m3_b - n_b·m3_a)/n. */
	{
		struct s128 prod_a = s128_mul_u64(b_snap.m3, n_a);
		struct s128 prod_b = s128_mul_u64(a->m3, n_b);
		struct s128 diff = s128_sub(prod_a, prod_b);
		diff = s128_mul_s64(diff, delta_int);
		diff = s128_div_u64(diff, n);
		diff = s128_mul_u64(diff, 4);
		m4_correction = s128_add(m4_correction, diff);
	}
	struct s128 m4_new = s128_add(a->m4, b_snap.m4);
	m4_new = s128_add(m4_new, m4_correction);

	a->n = n;
	a->m1_fp = m1_new_fp;
	a->m2 = m2_new;
	a->m3 = m3_new;
	a->m4 = m4_new;
}

/*
 * Readouts — convert fixed-point summary to double for reporting.
 * Userspace-only (FP rejected by BPF verifier in tracing context).
 */
#if !defined(__bpf__)

#include <math.h>

IOMOMENTS_BPF_INLINE double
iomoments_summary_bpf_mean_ns(const struct iomoments_summary_bpf *s)
{
	return (double)s->m1_fp / (double)(1ULL << IOMOMENTS_BPF_FRAC_BITS);
}

IOMOMENTS_BPF_INLINE double
iomoments_summary_bpf_variance_ns2(const struct iomoments_summary_bpf *s)
{
	if (s->n == 0) {
		return 0.0;
	}
	return (double)s->m2 / (double)s->n;
}

/*
 * Population skewness γ₁ = √n · M3 / M2^(3/2). Returns 0 on empty
 * summary or near-zero M2 (constant stream — skewness undefined).
 */
IOMOMENTS_BPF_INLINE double
iomoments_summary_bpf_skewness(const struct iomoments_summary_bpf *s)
{
	if (s->n == 0 || s->m2 <= 0) {
		return 0.0;
	}
	double nf = (double)s->n;
	double m2_d = (double)s->m2;
	double m3_d = s128_to_double(s->m3);
	return sqrt(nf) * m3_d / (m2_d * sqrt(m2_d));
}

/*
 * Population excess kurtosis γ₂ = n · M4 / M2² - 3. Zero for
 * Gaussian. Returns 0 on empty / near-zero-M2 summary.
 */
IOMOMENTS_BPF_INLINE double
iomoments_summary_bpf_excess_kurtosis(const struct iomoments_summary_bpf *s)
{
	if (s->n == 0 || s->m2 <= 0) {
		return 0.0;
	}
	double nf = (double)s->n;
	double m2_d = (double)s->m2;
	double m4_d = s128_to_double(s->m4);
	return nf * m4_d / (m2_d * m2_d) - 3.0;
}

#endif /* !__bpf__ */

#endif /* IOMOMENTS_PEBAY_BPF_H */
