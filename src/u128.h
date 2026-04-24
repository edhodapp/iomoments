/* SPDX-License-Identifier: GPL-2.0-only OR AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Multi-precision 64/128-bit arithmetic for BPF-safe code paths.
 *
 * The BPF verifier rejects compiler-emitted __multi3 / __divti3
 * libcalls that an __int128 expression expands to. But 64-bit ops
 * (add, sub, mul, shift, compare) are accepted by the verifier.
 * So: build 128-bit arithmetic by hand out of 64-bit primitives —
 * exactly what you'd do on an 8-bit CPU with no native word-wide
 * multiply.
 *
 * Scope today: the minimum set of primitives pebay_bpf.h's k=4
 * update needs (M3 and M4 accumulators, each ~2^83+ bits wide for
 * realistic workloads, can't fit s64). Specifically:
 *
 *   - struct s128: two's-complement 128-bit signed integer.
 *     hi = high 64 bits treated as signed; lo = low 64 bits unsigned.
 *   - s64_mul_s64: 64×64 → 128 signed multiply.
 *   - s128_add / s128_sub: carry/borrow-propagating 128-bit add/sub.
 *   - s128_to_double: readout to double at userspace reporting time.
 *
 * Not provided (explicit non-scope):
 *   - 128×128 multiply, 128/64 divide: pebay_bpf.h's k=4 update
 *     doesn't need them. Update divides δ by n (64/64 only);
 *     products that don't fit s64 land in s128 accumulators. Merge
 *     is computed in double-precision userspace from the s128
 *     accumulators via pebay.h's merge rule — no s128 division
 *     needed anywhere on the hot path.
 *   - Unsigned u128 variants: iomoments' higher moments are
 *     signed (m3 can go negative; m4 is mathematically non-negative
 *     but intermediate merge corrections can temporarily dip).
 *     Use s128 everywhere; readout checks positivity where it
 *     matters.
 *
 * Portability: compiles under -target bpf AND host clang/gcc.
 * Tests (tests/c/test_u128.c) validate each op against the compiler's
 * __int128 extension over boundary cases and a deterministic LCG
 * sample. __int128 is host-only — the test never compiles for BPF,
 * so using it there is fine.
 */

#ifndef IOMOMENTS_U128_H
#define IOMOMENTS_U128_H

/*
 * Type aliases instead of <stdint.h>: BPF compile (clang -target bpf)
 * pulls <stdint.h> → <gnu/stubs.h> which expects multiarch stubs that
 * aren't present by default on Ubuntu. Matches pebay_bpf.h convention.
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
 * Two's-complement 128-bit signed integer.
 *
 * Memory layout: {hi, lo} in source order; actual byte layout
 * follows the host ABI, but since this type is only manipulated
 * through the primitives in this header (no serialization across
 * processes/kernels), layout portability isn't a concern.
 *
 * Sign: `hi` carries the sign bit in two's-complement form. Negative
 * values have `hi` < 0; positive values have `hi` ≥ 0. `lo` is always
 * treated as unsigned (bits 0..63 of the 128-bit value), even for
 * negative numbers.
 */
struct s128 {
	iomoments_s64 hi;
	iomoments_u64 lo;
};

IOMOMENTS_BPF_INLINE struct s128 s128_from_s64(iomoments_s64 v)
{
	struct s128 r;
	r.lo = (iomoments_u64)v;
	/* Sign-extend: if v is negative, hi is all-ones; else all-zero. */
	r.hi = (v < 0) ? (iomoments_s64)-1LL : (iomoments_s64)0LL;
	return r;
}

IOMOMENTS_BPF_INLINE struct s128 s128_zero(void)
{
	struct s128 r = {0, 0};
	return r;
}

/*
 * s128 addition. Two's-complement arithmetic works cleanly across
 * the hi/lo split: treat `lo` as unsigned for carry detection, add
 * the carry into `hi` as if hi were unsigned (the signed view of the
 * result is still correct, because signed + signed in two's
 * complement is the same bit pattern as unsigned + unsigned of the
 * same width).
 */
IOMOMENTS_BPF_INLINE struct s128 s128_add(struct s128 a, struct s128 b)
{
	struct s128 r;
	r.lo = a.lo + b.lo;
	iomoments_u64 carry = (r.lo < a.lo) ? 1ULL : 0ULL;
	/* Cast hi's through unsigned for the add to avoid signed-overflow
	 * UB; the resulting bit pattern, reinterpreted as signed, is the
	 * correct two's-complement sum.
	 */
	iomoments_u64 hi_sum =
		(iomoments_u64)a.hi + (iomoments_u64)b.hi + carry;
	r.hi = (iomoments_s64)hi_sum;
	return r;
}

/*
 * s128 subtraction. Borrow detected on `lo` underflow; propagated
 * into `hi` via unsigned arithmetic to avoid signed-overflow UB.
 */
IOMOMENTS_BPF_INLINE struct s128 s128_sub(struct s128 a, struct s128 b)
{
	struct s128 r;
	r.lo = a.lo - b.lo;
	iomoments_u64 borrow = (a.lo < b.lo) ? 1ULL : 0ULL;
	iomoments_u64 hi_diff =
		(iomoments_u64)a.hi - (iomoments_u64)b.hi - borrow;
	r.hi = (iomoments_s64)hi_diff;
	return r;
}

/*
 * Unsigned 64×64 → 128 multiply via classic 4-partial split.
 *
 * Let a = A_hi·2³² + A_lo, b = B_hi·2³² + B_lo, each half < 2³².
 * Then a·b = A_hi·B_hi·2⁶⁴ + (A_hi·B_lo + A_lo·B_hi)·2³² + A_lo·B_lo.
 *
 * Each of the four 32×32 partials fits u64 (max (2³²−1)² < 2⁶⁴).
 * Assembly with carries:
 *   mid = (ll >> 32) + (hl & 0xFFFFFFFF) + (lh & 0xFFFFFFFF)
 *   lo  = (ll & 0xFFFFFFFF) | (mid << 32)
 *   hi  = hh + (hl >> 32) + (lh >> 32) + (mid >> 32)
 *
 * mid is bounded above by 3·(2³²−1) < 2³⁴, fits u64 with room; its
 * high half propagates into `hi` as the cross-term carry.
 *
 * Returns the 128-bit product packed as two u64 halves (returned
 * via out params; struct return would also work but two-u64 is
 * friendlier to the signed wrapper below).
 */
IOMOMENTS_BPF_INLINE void u64_mul_u64(iomoments_u64 a, iomoments_u64 b,
				      iomoments_u64 *out_hi,
				      iomoments_u64 *out_lo)
{
	iomoments_u64 a_lo = a & 0xFFFFFFFFULL;
	iomoments_u64 a_hi = a >> 32;
	iomoments_u64 b_lo = b & 0xFFFFFFFFULL;
	iomoments_u64 b_hi = b >> 32;

	iomoments_u64 ll = a_lo * b_lo;
	iomoments_u64 hl = a_hi * b_lo;
	iomoments_u64 lh = a_lo * b_hi;
	iomoments_u64 hh = a_hi * b_hi;

	iomoments_u64 mid =
		(ll >> 32) + (hl & 0xFFFFFFFFULL) + (lh & 0xFFFFFFFFULL);
	*out_lo = (ll & 0xFFFFFFFFULL) | (mid << 32);
	*out_hi = hh + (hl >> 32) + (lh >> 32) + (mid >> 32);
}

/*
 * Signed 64×64 → 128 multiply. Compute magnitude via unsigned
 * multiply, then negate the 128-bit result if the sign count is odd.
 *
 * Handles INT64_MIN correctly: `-(iomoments_s64)INT64_MIN` would be
 * signed-overflow UB, so take the two's-complement negation through
 * unsigned arithmetic (`~x + 1`) instead.
 */
IOMOMENTS_BPF_INLINE struct s128 s64_mul_s64(iomoments_s64 a, iomoments_s64 b)
{
	int a_neg = (a < 0);
	int b_neg = (b < 0);
	iomoments_u64 ua = a_neg ? (~(iomoments_u64)a + 1ULL)
				 : (iomoments_u64)a;
	iomoments_u64 ub = b_neg ? (~(iomoments_u64)b + 1ULL)
				 : (iomoments_u64)b;

	iomoments_u64 prod_hi, prod_lo;
	u64_mul_u64(ua, ub, &prod_hi, &prod_lo);

	struct s128 r;
	if (a_neg != b_neg) {
		/* Negate the 128-bit product (~x + 1 on both halves,
		 * propagating the carry from lo into hi).
		 */
		iomoments_u64 neg_lo = ~prod_lo + 1ULL;
		/* Same wraparound case as s128_to_double: cppcheck can't
		 * model the prod_lo == 0 branch where neg_lo == 0. */
		/* cppcheck-suppress knownConditionTrueFalse */
		iomoments_u64 carry = (neg_lo == 0) ? 1ULL : 0ULL;
		iomoments_u64 neg_hi = ~prod_hi + carry;
		r.hi = (iomoments_s64)neg_hi;
		r.lo = neg_lo;
	} else {
		r.hi = (iomoments_s64)prod_hi;
		r.lo = prod_lo;
	}
	return r;
}

/*
 * Userspace-only readouts. BPF targets reject floating-point
 * (the kernel doesn't save/restore FP registers across tracing
 * attach points), so these are excluded from the BPF compile.
 * BPF hot-path code accumulates s128 and hands the raw {hi, lo}
 * out via a map; userspace converts to double at reporting time.
 */
#if !defined(__bpf__)

/*
 * Convert s128 to double. Used at userspace readout time.
 *
 * Naive (double)hi · 2⁶⁴ + (double)lo loses precision near zero for
 * negatives: e.g., s128{-1, 0xFFFF...FFFF} represents −1, but the
 * naive sum is (−1)·2⁶⁴ + 2⁶⁴ (rounded) = 0. (double)lo rounds up to
 * 2⁶⁴ for lo = 0xFFFF...FFFF, cancelling against −2⁶⁴ exactly.
 *
 * Fix: extract the magnitude via two's-complement negation when hi
 * is negative, convert the magnitude positively, and negate the
 * final double. The magnitude sum uses purely positive operands, so
 * no catastrophic cancellation.
 *
 * Precision: double has 53 mantissa bits. Values >2⁵³ lose low-bit
 * precision. For iomoments moments (M3, M4), the readout is used to
 * form dimensionless ratios (skewness, kurtosis) where relative
 * precision dominates; 2⁻⁵³ ≈ 1.1e-16 is well below what any real
 * workload will resolve.
 */
IOMOMENTS_BPF_INLINE double s128_to_double(struct s128 v)
{
	/* 2^64 is exactly representable in double (power of 2 in
	 * double's exponent range). */
	const double two_64 = 18446744073709551616.0;
	if (v.hi < 0) {
		iomoments_u64 neg_lo = ~v.lo + 1ULL;
		/* neg_lo == 0 when v.lo == 0 (the carry case); cppcheck's
		 * data-flow can't model the u64 wraparound. */
		/* cppcheck-suppress knownConditionTrueFalse */
		iomoments_u64 carry = (neg_lo == 0) ? 1ULL : 0ULL;
		iomoments_u64 neg_hi = ~(iomoments_u64)v.hi + carry;
		return -((double)neg_hi * two_64 + (double)neg_lo);
	}
	return (double)(iomoments_u64)v.hi * two_64 + (double)v.lo;
}

#endif /* !__bpf__ */

#endif /* IOMOMENTS_U128_H */
