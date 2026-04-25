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
 * Scope: the minimum set of primitives pebay_bpf.h's k=4 update
 * needs (M3 and M4 accumulators, each ~2^83+ bits wide for
 * realistic workloads, can't fit s64). Specifically:
 *
 *   - struct s128: two's-complement 128-bit signed integer.
 *     hi = high 64 bits treated as signed; lo = low 64 bits unsigned.
 *   - s64_mul_s64: 64×64 → 128 signed multiply.
 *   - s128_add / s128_sub: carry/borrow-propagating 128-bit add/sub.
 *   - s128_mul_u64 / s128_mul_s64: 128 × 64 multiplies (signed
 *     numerator), truncating bits ≥ 128.
 *   - s128_div_u64: 128 / 64 → 128 division via Knuth Algorithm D
 *     (Hacker's Delight 9-3). Signed numerator, positive divisor.
 *   - s128_to_double: readout to double at userspace reporting time.
 *
 * Not provided (explicit non-scope):
 *   - 128×128 multiply: pebay_bpf.h's k=4 update never needs it.
 *     The largest products are s64×s128 (handled by s128_mul_s64).
 *   - 128/128 divide: same — the only division is by the sample
 *     count n (always u64).
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
 * Multiply s128 by an unsigned u64, truncating bits >= 128.
 *
 *   v · m = (v_hi · 2⁶⁴ + v_lo) · m
 *         = v_hi · m · 2⁶⁴ + v_lo · m
 *
 * The v_lo · m product is the full 128 low bits; v_hi · m supplies
 * up to 128 more bits, but only its low 64 land in our result's
 * `hi` (its high 64 are bits >= 192, truncated).
 *
 * Sign: caller's `v` is signed. For positive v.hi the unsigned high
 * half is just (u64)v.hi. For negative v, take magnitude via
 * two's-complement negation, multiply, restore sign — same pattern
 * as s64_mul_s64.
 */
IOMOMENTS_BPF_INLINE struct s128 s128_mul_u64(struct s128 v, iomoments_u64 m)
{
	int neg = (v.hi < 0);
	iomoments_u64 mag_hi = (iomoments_u64)v.hi;
	iomoments_u64 mag_lo = v.lo;
	if (neg) {
		mag_lo = ~v.lo + 1ULL;
		/* cppcheck-suppress knownConditionTrueFalse */
		iomoments_u64 carry = (mag_lo == 0) ? 1ULL : 0ULL;
		mag_hi = ~(iomoments_u64)v.hi + carry;
	}

	/* Two u64 × u64 → u128 partials; assemble truncating to 128. */
	iomoments_u64 lo_hi, lo_lo;
	iomoments_u64 hi_hi_unused, hi_lo;
	u64_mul_u64(mag_lo, m, &lo_hi, &lo_lo);
	u64_mul_u64(mag_hi, m, &hi_hi_unused, &hi_lo);
	(void)hi_hi_unused;
	iomoments_u64 prod_lo = lo_lo;
	iomoments_u64 prod_hi = lo_hi + hi_lo;

	struct s128 r;
	if (neg) {
		iomoments_u64 neg_lo = ~prod_lo + 1ULL;
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
 * Multiply s128 by a signed s64. Sign of the result is the XOR of
 * the input signs.
 *
 * Implemented as a thin wrapper over s128_mul_u64: factor out m's
 * sign, multiply by |m| as u64, conditionally negate the result.
 * Faster than building a fully-signed s128_mul_s128 since one
 * operand is known to fit s64 — reuses the truncating-multiply
 * already provided.
 */
IOMOMENTS_BPF_INLINE struct s128 s128_mul_s64(struct s128 v, iomoments_s64 m)
{
	int m_neg = (m < 0);
	iomoments_u64 abs_m = m_neg ? (~(iomoments_u64)m + 1ULL)
				    : (iomoments_u64)m;
	struct s128 prod = s128_mul_u64(v, abs_m);
	if (!m_neg) {
		return prod;
	}
	/* Negate prod (~x + 1 across both halves with carry from lo). */
	iomoments_u64 neg_lo = ~prod.lo + 1ULL;
	/* cppcheck-suppress knownConditionTrueFalse */
	iomoments_u64 carry = (neg_lo == 0) ? 1ULL : 0ULL;
	iomoments_u64 neg_hi = ~(iomoments_u64)prod.hi + carry;
	struct s128 r;
	r.hi = (iomoments_s64)neg_hi;
	r.lo = neg_lo;
	return r;
}

/*
 * Count leading zero bits of a u64. Used by the Knuth-D divide to
 * normalize the divisor.
 *
 * Implemented as a binary search rather than a 64-iteration loop:
 * 6 conditional shifts, ~30 instructions, fully branch-bounded for
 * the verifier. Don't use __builtin_clzll under BPF — it can
 * lower to the `clz` machine op on hosts but is not guaranteed
 * available across BPF target configurations and the verifier
 * doesn't always see through the intrinsic the way it sees through
 * plain C arithmetic.
 *
 * For x == 0, returns 64 (no leading zero count is well-defined,
 * so we return the bit width). Caller never passes 0 in our use
 * (divisor is always nonzero), but defining the boundary makes
 * the helper composable.
 */
IOMOMENTS_BPF_INLINE int u64_clz(iomoments_u64 x)
{
	int n = 0;
	if (x == 0) {
		return 64;
	}
	if ((x >> 32) == 0) {
		n += 32;
		x <<= 32;
	}
	if ((x >> 48) == 0) {
		n += 16;
		x <<= 16;
	}
	if ((x >> 56) == 0) {
		n += 8;
		x <<= 8;
	}
	if ((x >> 60) == 0) {
		n += 4;
		x <<= 4;
	}
	if ((x >> 62) == 0) {
		n += 2;
		x <<= 2;
	}
	if ((x >> 63) == 0) {
		n += 1;
	}
	return n;
}

/*
 * Unsigned 128/64 → 64 division, precondition u_hi < d.
 *
 * Hacker's Delight section 9-3 (a tightening of Knuth Algorithm D
 * for the case where the divisor fits in two 32-bit limbs and the
 * dividend in four). Knuth's analysis proves that for a normalized
 * divisor (top bit set), the q̂ estimate is at most 2 too large, so
 * each refinement loop runs ≤ 2 iterations — no while-loop, just a
 * for-loop with an explicit upper bound (verifier-friendly).
 *
 * Why this beats shift-and-subtract: 128 inner iterations collapse
 * to 2 outer iterations (one per 32-bit quotient digit), each
 * doing ~3 multiplies and a couple of subtracts. Roughly 30×
 * faster on a measured BPF hot path.
 *
 * The intermediate `un21 = (un32 << 32) + un1 - q1·d` step would
 * need 96-bit arithmetic to compute exactly, but the final un21
 * fits u64 by Knuth's correctness proof. So the natural u64 wrap
 * of the expression yields the correct un21 (mod 2⁶⁴ matches the
 * true value because the lost high bits cancel exactly).
 */
IOMOMENTS_BPF_INLINE iomoments_u64 u128_div_u64_inner(iomoments_u64 u_hi,
						      iomoments_u64 u_lo,
						      iomoments_u64 d)
{
	const iomoments_u64 b = 1ULL << 32; /* base for 32-bit limbs */

	int s = u64_clz(d);
	d <<= s;

	/* Shift u left by s. When s == 0 the bit-shift `u_lo >> (64-s)`
	 * would be `>> 64` (UB); guard with the s==0 branch. */
	iomoments_u64 un_hi, un_lo;
	if (s == 0) {
		un_hi = u_hi;
		un_lo = u_lo;
	} else {
		un_hi = (u_hi << s) | (u_lo >> (64 - s));
		un_lo = u_lo << s;
	}

	iomoments_u64 vn1 = d >> 32;
	iomoments_u64 vn0 = d & 0xFFFFFFFFULL;
	iomoments_u64 un32 = un_hi;
	iomoments_u64 un1 = un_lo >> 32;
	iomoments_u64 un0 = un_lo & 0xFFFFFFFFULL;

	/* First quotient digit q1. */
	iomoments_u64 q1 = un32 / vn1;
	iomoments_u64 rhat = un32 - q1 * vn1;
	for (int i = 0; i < 2; i++) {
		int too_big = (q1 >= b);
		if (!too_big) {
			too_big = (q1 * vn0 > b * rhat + un1);
		}
		if (!too_big) {
			break;
		}
		q1 -= 1;
		rhat += vn1;
		if (rhat >= b) {
			break;
		}
	}

	/* Multi-precision subtract via natural u64 wrap (see comment
	 * above): correct mod 2⁶⁴, and Knuth proves the true value
	 * fits u64. */
	iomoments_u64 un21 = (un32 << 32) + un1 - q1 * d;

	/* Second quotient digit q0. */
	iomoments_u64 q0 = un21 / vn1;
	rhat = un21 - q0 * vn1;
	for (int i = 0; i < 2; i++) {
		int too_big = (q0 >= b);
		if (!too_big) {
			too_big = (q0 * vn0 > b * rhat + un0);
		}
		if (!too_big) {
			break;
		}
		q0 -= 1;
		rhat += vn1;
		if (rhat >= b) {
			break;
		}
	}

	return (q1 << 32) | q0;
}

/*
 * Divide s128 by a positive u64, returning s128 quotient.
 *
 * Algorithm: Hacker's Delight 9-3 / Knuth D, two-step:
 *   1. q_hi = u_hi / d  (native u64/u64).
 *   2. q_lo = (r:u_lo) / d  where r = u_hi % d (so r < d, quotient
 *      fits u64) — solved by u128_div_u64_inner.
 *
 * Caller contract: d != 0. Hot path divides by n (sample count),
 * always ≥ 1 by construction.
 *
 * Sign: signed numerator, unsigned divisor. Take magnitude, divide
 * unsigned, restore sign.
 *
 * Truncation: integer division rounds toward zero (consistent with
 * C's signed-division semantics post-C99).
 */
IOMOMENTS_BPF_INLINE struct s128 s128_div_u64(struct s128 v, iomoments_u64 d)
{
	int neg = (v.hi < 0);
	iomoments_u64 num_hi = (iomoments_u64)v.hi;
	iomoments_u64 num_lo = v.lo;
	if (neg) {
		num_lo = ~v.lo + 1ULL;
		/* cppcheck-suppress knownConditionTrueFalse */
		iomoments_u64 carry = (num_lo == 0) ? 1ULL : 0ULL;
		num_hi = ~(iomoments_u64)v.hi + carry;
	}

	iomoments_u64 q_hi, q_lo;
	if (num_hi == 0) {
		/* Quotient fits u64 trivially: just one u64/u64 division. */
		q_hi = 0;
		q_lo = num_lo / d;
	} else if (num_hi < d) {
		/* Quotient still fits u64; full Knuth-D needed for the lo
		 * half (numerator is the 128-bit (num_hi:num_lo)). */
		q_hi = 0;
		q_lo = u128_div_u64_inner(num_hi, num_lo, d);
	} else {
		/* Quotient exceeds u64: split into two Knuth-D divides.
		 * The first computes q_hi = num_hi / d (a 64/64); the
		 * second feeds (num_hi % d : num_lo) to Knuth-D for q_lo. */
		q_hi = num_hi / d;
		iomoments_u64 r1 = num_hi - q_hi * d;
		q_lo = u128_div_u64_inner(r1, num_lo, d);
	}

	struct s128 result;
	if (neg) {
		iomoments_u64 neg_lo = ~q_lo + 1ULL;
		/* cppcheck-suppress knownConditionTrueFalse */
		iomoments_u64 carry = (neg_lo == 0) ? 1ULL : 0ULL;
		iomoments_u64 neg_hi = ~q_hi + carry;
		result.hi = (iomoments_s64)neg_hi;
		result.lo = neg_lo;
	} else {
		result.hi = (iomoments_s64)q_hi;
		result.lo = q_lo;
	}
	return result;
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
