/* SPDX-License-Identifier: GPL-2.0-only OR AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Pébay (2008) online moment update — BPF-safe fixed-point variant
 * at k=2 (Welford). Companion to src/pebay.h, which uses double.
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
 * Scale choice (D011 Phase 2, decided 2026-04-24 at first-use):
 * -----------------------------------------------------------------
 *
 *   m1 — running mean
 *        iomoments_s64 in Q32.32 fixed-point ns.
 *        Integer part up to 2^31 ns (~2.1 s) — above realistic I/O
 *        latency except for catastrophic stalls (which are diagnostic
 *        signals themselves, handled separately).
 *        Fractional part: 32 bits ~= 2.3e-10 ns. More than enough
 *        headroom for the delta/n update to retain precision as n
 *        grows.
 *
 *   m2 — running sum of squared deviations
 *        iomoments_s64 in raw ns² (Q0.0, no fractional bits).
 *        Update contribution per sample is roughly σ² ns². Over N
 *        samples, m2 grows to ~N·σ². Max int64 is ~9.2e18; that
 *        bounds N·σ² before overflow. For σ=1ms samples (σ²=1e12),
 *        overflow threshold N ≈ 9e6. For σ=10μs, N ≈ 9e10.
 *
 *        Overflow is a documented limitation. Userspace's reporting
 *        loop must snapshot-and-reset each per-CPU summary before
 *        saturation. An int128-emulated m2 (two iomoments_u64 with carry
 *        propagation) is a follow-up if real usage pressures this.
 *
 *   x  — input sample (iomoments_u64 ns, directly from bpf_ktime_get_ns).
 *        No scaling on input; iomoments works at ns granularity.
 *
 * -----------------------------------------------------------------
 * Precision vs pebay.h
 * -----------------------------------------------------------------
 *
 * For integer-ns inputs, pebay_bpf's running mean agrees with
 * pebay's to within one Q32.32 fractional bit (~2.3e-10 ns) — well
 * below measurement precision.
 *
 * m2 agrees to within the integer-truncation error accumulated
 * during the delta_fp → delta_int downshift in the update. For N
 * samples that's bounded by N ns² in the worst case. variance =
 * m2/N, so relative precision scales as 1/σ² — very good for
 * distributions with σ >> 1 ns, converging to "iomoments
 * precision = measurement precision" at ns-integer inputs.
 *
 * -----------------------------------------------------------------
 * Footprint
 * -----------------------------------------------------------------
 *
 * struct iomoments_summary_bpf = 24 bytes (n + m1_fp + m2). Fits
 * well under D009's 64-byte per_cpu_update_bytes budget at k=2.
 * M3/M4 extension will push this past 64; revisit the budget then.
 */

#ifndef IOMOMENTS_PEBAY_BPF_H
#define IOMOMENTS_PEBAY_BPF_H

/*
 * Type aliases instead of <stdint.h>: BPF compile (clang -target bpf)
 * pulls <stdint.h> → <gnu/stubs.h> which expects the 32-bit multiarch
 * stubs (gnu/stubs-32.h) that aren't installed by default on Ubuntu
 * unless gcc-multilib is pulled in. Avoiding stdint.h here keeps
 * pebay_bpf.h standalone and BPF-host-agnostic. unsigned long long
 * and long long are 64-bit on every target iomoments ships to.
 */
typedef unsigned long long iomoments_u64;
typedef long long iomoments_s64;

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

/*
 * m2 stored as int64 in raw ns² (Q0.0). Welford's ideal update
 * needs Q64.64 accumulation (Q32.32 * Q32.32 product) which exceeds
 * int64 and requires __int128 — but BPF targets reject __multi3
 * (128-bit multiply compiler builtin). Draft-first compromise: drop
 * the fractional-ns² contribution on each update by computing the
 * product at integer-ns resolution via `delta_fp >> 32`. Accumulated
 * error vs pebay.h is bounded by 1 ns² per update and scales as
 * 1/σ² relative to variance — negligible for σ >> 1 ns (i.e. every
 * real iomoments workload), but visible on integer-small textbook
 * fixtures like [2,4,4,4,5,5,7,9] where "true" variance is 4 and
 * the fixed-point version reads ≈3.625.
 *
 * A follow-up commit can tighten this by implementing manual 64×64→
 * 128 multiplication using BPF-safe 64-bit ops (ah·bh << 64 +
 * ah·bl << 32 + al·bh << 32 + al·bl). Deferred until a real
 * workload pressures the precision floor.
 */
struct iomoments_summary_bpf {
	iomoments_u64 n;
	iomoments_s64 m1_fp; /* Q32.32 signed ns */
	iomoments_s64 m2; /* raw ns² — integer-approximate Welford accumulator */
};

#define IOMOMENTS_SUMMARY_BPF_ZERO                                             \
	{                                                                      \
		0, 0, 0                                                        \
	}

IOMOMENTS_BPF_INLINE void
iomoments_summary_bpf_init(struct iomoments_summary_bpf *s)
{
	s->n = 0;
	s->m1_fp = 0;
	s->m2 = 0;
}

/*
 * Incorporate one sample x (nanoseconds integer) into the summary.
 *
 * Fixed-point translation of Welford:
 *
 *   n'      = n + 1
 *   x_fp    = x << 32                  (lift sample to Q32.32)
 *   δ_fp    = x_fp - m1_fp              (Q32.32 residual pre-update)
 *   m1_fp  += δ_fp / n'                 (integer division; rounds
 *                                         toward zero with ≤ 1/2^32 ns
 *                                         truncation per step)
 *   δ'_fp   = x_fp - m1_fp              (Q32.32 residual post-update)
 *   product = (δ_fp >> 32) * (δ'_fp >> 32)
 *                                       (Q0.0 ns² — drop fractional
 *                                         bits before multiplying so
 *                                         the product fits int64 for
 *                                         realistic sample ranges)
 *   m2     += product
 *
 * The ≥32-bit upshift on x assumes x < 2^31 ns (~2.1 s). Larger
 * single-sample latencies overflow x_fp. Caller must keep samples
 * under that bound; iomoments' probe phase rejects samples beyond
 * realistic thresholds anyway (diagnostic signal).
 */
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

IOMOMENTS_BPF_INLINE void
iomoments_summary_bpf_update(struct iomoments_summary_bpf *s, iomoments_u64 x)
{
	s->n += 1;
	iomoments_s64 x_fp = (iomoments_s64)(x << IOMOMENTS_BPF_FRAC_BITS);
	iomoments_s64 delta_fp = x_fp - s->m1_fp;
	iomoments_s64 delta_n_fp = iomoments_bpf_signed_div(delta_fp, s->n);
	s->m1_fp += delta_n_fp;
	iomoments_s64 delta_post_fp = x_fp - s->m1_fp;
	/*
	 * Compute product at integer-ns resolution: shift each Q32.32
	 * delta down to integer ns before multiplying. Keeps everything
	 * in 64-bit signed arithmetic (BPF-safe). Sub-ns fractional
	 * contributions are discarded — see struct comment for the
	 * precision tradeoff.
	 */
	iomoments_s64 delta_int = delta_fp >> IOMOMENTS_BPF_FRAC_BITS;
	iomoments_s64 delta_post_int = delta_post_fp >> IOMOMENTS_BPF_FRAC_BITS;
	s->m2 += delta_int * delta_post_int;
}

/*
 * Merge partial summary `b` into `a` in place.
 *
 * Pébay eq. (3.2)/(3.3) at k=2, translated to fixed-point:
 *
 *   n      = n_a + n_b
 *   δ_fp   = m1_b - m1_a                 (Q32.32)
 *   m1_new_fp = (n_a·m1_a_fp + n_b·m1_b_fp) / n
 *   correction = (n_a · n_b · (δ_int)²) / n
 *                                         (Q0.0 ns², integer math)
 *   m2_new = m2_a + m2_b + correction
 *
 * Aliasing-safe (caller may pass &s, &s): snapshot `b` before
 * writing `a`, matching pebay.h's convention.
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
	iomoments_s64 delta_fp = b_snap.m1_fp - a->m1_fp;
	iomoments_s64 delta_int = delta_fp >> IOMOMENTS_BPF_FRAC_BITS;
	/*
	 * Merge-time weighted mean: work at integer-ns resolution to
	 * stay under int64 overflow for realistic n.
	 *   m1_new_int = (m1_a_int·n_a + m1_b_int·n_b) / n
	 *   m1_new_fp  = m1_new_int << 32
	 * Loses the Q32.32 fractional contributions of each summary's
	 * m1 during merge. Acceptable for per-CPU aggregation where
	 * each contributor already has n·σ-scale precision; catastrophic
	 * only when merging many tiny summaries (sub-microsecond n),
	 * which isn't iomoments' aggregation model.
	 */
	iomoments_s64 m1_a_int = a->m1_fp >> IOMOMENTS_BPF_FRAC_BITS;
	iomoments_s64 m1_b_int = b_snap.m1_fp >> IOMOMENTS_BPF_FRAC_BITS;
	iomoments_s64 m1_new_int_num = m1_a_int * (iomoments_s64)a->n +
				       m1_b_int * (iomoments_s64)b_snap.n;
	iomoments_s64 m1_new_int = iomoments_bpf_signed_div(m1_new_int_num, n);
	iomoments_s64 m1_new_fp = m1_new_int << IOMOMENTS_BPF_FRAC_BITS;
	/*
	 * Pébay correction: δ_int² · n_a · n_b / n in integer-ns². All
	 * factors fit int64 for realistic iomoments n and δ. Same
	 * integer-ns precision caveat as the update.
	 */
	iomoments_u64 n_product = a->n * b_snap.n;
	iomoments_s64 correction =
		(iomoments_s64)(n_product / n) * (delta_int * delta_int);
	a->n = n;
	a->m1_fp = m1_new_fp;
	a->m2 = a->m2 + b_snap.m2 + correction;
}

/*
 * Readouts — convert fixed-point summary to double for reporting.
 * The userspace consumer calls these after reading the per-CPU
 * summary out of a BPF map. Doubles are OK here: readout is
 * userspace.
 */
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

#endif /* IOMOMENTS_PEBAY_BPF_H */
