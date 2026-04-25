/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Validates src/u128.h's hand-rolled 128-bit primitives against the
 * compiler's __int128 extension. __int128 is host-only — BPF targets
 * reject its libcalls — but this test never compiles for BPF, so
 * using it as the oracle here is fine.
 *
 * Coverage:
 *   - u64_mul_u64 (unsigned 64×64 → 128)
 *   - s64_mul_s64 (signed 64×64 → 128)
 *   - s128_add / s128_sub
 *   - s128_from_s64, s128_zero
 *   - s128_to_double
 *
 * For each primitive: boundary cases (zeros, ones, INT64_MIN/MAX,
 * UINT64_MAX, signs) + a deterministic LCG sweep of 2000 random-ish
 * inputs. Determinism matters so a failure is reproducible without
 * pickling seeds.
 *
 * Exit code: 0 = all pass; 1 = at least one failure.
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "../../src/u128.h"

static int failures;

#define FAIL_FMT(...)                                                          \
	do {                                                                   \
		fprintf(stderr, "FAIL %s:%d  ", __FILE__, __LINE__);           \
		fprintf(stderr, __VA_ARGS__);                                  \
		failures += 1;                                                 \
	} while (0)

typedef __int128 s128_ref;
typedef unsigned __int128 u128_ref;

static iomoments_u64 ref_lo(u128_ref v)
{
	return (iomoments_u64)(v & (u128_ref)0xFFFFFFFFFFFFFFFFULL);
}

static iomoments_u64 ref_hi(u128_ref v)
{
	return (iomoments_u64)(v >> 64);
}

static int u64_pair_eq_ref(iomoments_u64 got_hi, iomoments_u64 got_lo,
			   u128_ref expected)
{
	return got_hi == ref_hi(expected) && got_lo == ref_lo(expected);
}

static int s128_eq_ref(struct s128 got, s128_ref expected)
{
	u128_ref u = (u128_ref)expected;
	return got.lo == ref_lo(u) && (iomoments_u64)got.hi == ref_hi(u);
}

/* Deterministic LCG (same constants as glibc rand48 family). */
static iomoments_u64 lcg_next(iomoments_u64 *state)
{
	*state = (*state) * 6364136223846793005ULL + 1442695040888963407ULL;
	return *state;
}

/* --- u64_mul_u64 ------------------------------------------------------- */

static void test_u64_mul_u64_boundaries(void)
{
	struct {
		iomoments_u64 a, b;
	} cases[] = {
		{0, 0},
		{0, 1},
		{1, 1},
		{0xFFFFFFFFFFFFFFFFULL, 1},
		{1, 0xFFFFFFFFFFFFFFFFULL},
		{0xFFFFFFFFFFFFFFFFULL, 0xFFFFFFFFFFFFFFFFULL},
		/* 2^32 · 2^32 = 2^64: result straddles the half boundary. */
		{0x100000000ULL, 0x100000000ULL},
		/* Near-half cases to exercise carry propagation. */
		{0xFFFFFFFFULL, 0xFFFFFFFFULL},
		{0x1FFFFFFFFULL, 0xFFFFFFFFULL},
		{0x123456789ABCDEF0ULL, 0xFEDCBA9876543210ULL},
	};
	const size_t n = sizeof(cases) / sizeof(cases[0]);
	for (size_t i = 0; i < n; i++) {
		u128_ref expected = (u128_ref)cases[i].a * cases[i].b;
		iomoments_u64 got_hi, got_lo;
		u64_mul_u64(cases[i].a, cases[i].b, &got_hi, &got_lo);
		if (!u64_pair_eq_ref(got_hi, got_lo, expected)) {
			FAIL_FMT("u64_mul_u64(%llu, %llu) got "
				 "hi=%llx lo=%llx, expected hi=%llx lo=%llx\n",
				 (unsigned long long)cases[i].a,
				 (unsigned long long)cases[i].b,
				 (unsigned long long)got_hi,
				 (unsigned long long)got_lo,
				 (unsigned long long)ref_hi(expected),
				 (unsigned long long)ref_lo(expected));
		}
	}
}

static void test_u64_mul_u64_lcg_sweep(void)
{
	iomoments_u64 state = 0xDEADBEEFCAFEBABEULL;
	for (int trial = 0; trial < 2000; trial++) {
		iomoments_u64 a = lcg_next(&state);
		iomoments_u64 b = lcg_next(&state);
		u128_ref expected = (u128_ref)a * b;
		iomoments_u64 got_hi, got_lo;
		u64_mul_u64(a, b, &got_hi, &got_lo);
		if (!u64_pair_eq_ref(got_hi, got_lo, expected)) {
			FAIL_FMT("u64_mul_u64 LCG trial %d: a=%llu b=%llu\n",
				 trial, (unsigned long long)a,
				 (unsigned long long)b);
			return;
		}
	}
}

/* --- s64_mul_s64 ------------------------------------------------------- */

static void test_s64_mul_s64_boundaries(void)
{
	const iomoments_s64 S64_MIN = (iomoments_s64)0x8000000000000000ULL;
	const iomoments_s64 S64_MAX = (iomoments_s64)0x7FFFFFFFFFFFFFFFULL;
	struct {
		iomoments_s64 a, b;
	} cases[] = {
		{0, 0},
		{0, 1},
		{1, -1},
		{-1, -1},
		{S64_MAX, S64_MAX},
		{S64_MIN, S64_MIN},
		{S64_MIN, 1},
		/* INT64_MIN · -1 overflows s64 if done naively; s128 result
		 * is +2^63 and must be exact. */
		{S64_MIN, -1},
		{S64_MAX, -1},
		{-(iomoments_s64)1000000000LL, (iomoments_s64)1000000000LL},
		{(iomoments_s64)0x123456789ABCDEF0LL,
		 -(iomoments_s64)0x0FEDCBA987654321LL},
	};
	const size_t n = sizeof(cases) / sizeof(cases[0]);
	for (size_t i = 0; i < n; i++) {
		s128_ref expected = (s128_ref)cases[i].a * cases[i].b;
		struct s128 got = s64_mul_s64(cases[i].a, cases[i].b);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s64_mul_s64(%lld, %lld) got hi=%llx "
				 "lo=%llx\n",
				 (long long)cases[i].a, (long long)cases[i].b,
				 (unsigned long long)got.hi,
				 (unsigned long long)got.lo);
		}
	}
}

static void test_s64_mul_s64_lcg_sweep(void)
{
	iomoments_u64 state = 0xCAFED00D12345678ULL;
	for (int trial = 0; trial < 2000; trial++) {
		iomoments_s64 a = (iomoments_s64)lcg_next(&state);
		iomoments_s64 b = (iomoments_s64)lcg_next(&state);
		s128_ref expected = (s128_ref)a * b;
		struct s128 got = s64_mul_s64(a, b);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s64_mul_s64 LCG trial %d: a=%lld b=%lld\n",
				 trial, (long long)a, (long long)b);
			return;
		}
	}
}

/* --- s128_add / s128_sub ---------------------------------------------- */

static struct s128 s128_from_ref(s128_ref v)
{
	u128_ref u = (u128_ref)v;
	struct s128 r;
	r.lo = ref_lo(u);
	r.hi = (iomoments_s64)ref_hi(u);
	return r;
}

static void test_s128_add_lcg_sweep(void)
{
	iomoments_u64 state = 0xBADC0FFEE0DDF00DULL;
	for (int trial = 0; trial < 2000; trial++) {
		/* Build s128 operands by concatenating two u64s from the LCG;
		 * covers the full s128 range. */
		iomoments_u64 a_hi = lcg_next(&state);
		iomoments_u64 a_lo = lcg_next(&state);
		iomoments_u64 b_hi = lcg_next(&state);
		iomoments_u64 b_lo = lcg_next(&state);
		s128_ref a_ref = (s128_ref)(((u128_ref)a_hi << 64) | a_lo);
		s128_ref b_ref = (s128_ref)(((u128_ref)b_hi << 64) | b_lo);
		s128_ref expected = a_ref + b_ref;
		struct s128 a = s128_from_ref(a_ref);
		struct s128 b = s128_from_ref(b_ref);
		struct s128 got = s128_add(a, b);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s128_add LCG trial %d\n", trial);
			return;
		}
	}
}

static void test_s128_sub_lcg_sweep(void)
{
	iomoments_u64 state = 0x0123456789ABCDEFULL;
	for (int trial = 0; trial < 2000; trial++) {
		iomoments_u64 a_hi = lcg_next(&state);
		iomoments_u64 a_lo = lcg_next(&state);
		iomoments_u64 b_hi = lcg_next(&state);
		iomoments_u64 b_lo = lcg_next(&state);
		s128_ref a_ref = (s128_ref)(((u128_ref)a_hi << 64) | a_lo);
		s128_ref b_ref = (s128_ref)(((u128_ref)b_hi << 64) | b_lo);
		s128_ref expected = a_ref - b_ref;
		struct s128 a = s128_from_ref(a_ref);
		struct s128 b = s128_from_ref(b_ref);
		struct s128 got = s128_sub(a, b);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s128_sub LCG trial %d\n", trial);
			return;
		}
	}
}

static void test_s128_add_boundaries(void)
{
	/* Carry from lo into hi. */
	struct s128 a = {0, 0xFFFFFFFFFFFFFFFFULL};
	struct s128 b = {0, 1};
	struct s128 got = s128_add(a, b);
	s128_ref expected = (s128_ref)((u128_ref)1 << 64);
	if (!s128_eq_ref(got, expected)) {
		FAIL_FMT("s128_add carry-from-lo failed\n");
	}
	/* Borrow from hi into lo. */
	struct s128 c = {1, 0};
	struct s128 d = {0, 1};
	struct s128 got2 = s128_sub(c, d);
	s128_ref expected2 = (s128_ref)0xFFFFFFFFFFFFFFFFULL;
	if (!s128_eq_ref(got2, expected2)) {
		FAIL_FMT("s128_sub borrow-from-hi failed\n");
	}
	/* Negative + negative stays negative. */
	struct s128 neg1 = s128_from_s64(-1);
	struct s128 got3 = s128_add(neg1, neg1);
	if (!s128_eq_ref(got3, (s128_ref)-2)) {
		FAIL_FMT("s128_add -1 + -1 failed\n");
	}
	/* Positive + negative crossing zero (the typical k=4 m3-correction
	 * pattern: cancel a δ³·X term against another). */
	struct s128 big_pos =
		s128_from_s64((iomoments_s64)0x123456789ABCDEF0LL);
	struct s128 big_neg =
		s128_from_s64(-(iomoments_s64)0x123456789ABCDEF0LL);
	struct s128 got4 = s128_add(big_pos, big_neg);
	if (got4.hi != 0 || got4.lo != 0) {
		FAIL_FMT("s128_add(+x, -x) ≠ 0: hi=%llx lo=%llx\n",
			 (unsigned long long)got4.hi,
			 (unsigned long long)got4.lo);
	}
}

/* --- s128_from_s64, s128_zero ---------------------------------------- */

static void test_s128_from_s64(void)
{
	const iomoments_s64 S64_MIN = (iomoments_s64)0x8000000000000000ULL;
	iomoments_s64 cases[] = {
		0,  1,	-1, S64_MIN, (iomoments_s64)0x7FFFFFFFFFFFFFFFLL,
		42, -42};
	for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		struct s128 got = s128_from_s64(cases[i]);
		if (!s128_eq_ref(got, (s128_ref)cases[i])) {
			FAIL_FMT("s128_from_s64(%lld) failed\n",
				 (long long)cases[i]);
		}
	}
	struct s128 z = s128_zero();
	if (z.hi != 0 || z.lo != 0) {
		FAIL_FMT("s128_zero returned nonzero\n");
	}
}

/* --- s128_to_double --------------------------------------------------- */

static void test_s128_to_double(void)
{
	/* Most-negative s128 = -2^127, named boundary case for the
	 * negate-and-convert path: requires the carry-on-zero in the
	 * negation logic to fire. */
	{
		struct s128 v = {(iomoments_s64)0x8000000000000000ULL, 0};
		double got = s128_to_double(v);
		double expected = -170141183460469231731687303715884105728.0;
		if (got != expected) {
			FAIL_FMT("s128_to_double(s128_min) got %g expected "
				 "%g\n",
				 got, expected);
		}
	}
	/* Values exactly representable in double (|v| ≤ 2^53): result
	 * must be bit-exact. */
	s128_ref exact_cases[] = {
		0,
		1,
		-1,
		42,
		-42,
		(s128_ref)((u128_ref)1 << 52),
		-(s128_ref)((u128_ref)1 << 52),
	};
	for (size_t i = 0; i < sizeof(exact_cases) / sizeof(exact_cases[0]);
	     i++) {
		double got = s128_to_double(s128_from_ref(exact_cases[i]));
		double expected = (double)exact_cases[i];
		if (got != expected) {
			FAIL_FMT("s128_to_double exact case %zu: got %f "
				 "expected %f\n",
				 i, got, expected);
		}
	}
	/* Large-magnitude values: check relative agreement with the
	 * compiler's own (double)__int128 conversion within 1 ULP.
	 * Both paths round to double independently, so agreement to 1
	 * ULP is the correct specification. */
	iomoments_u64 state = 0xFACEB00C00BAF10DULL;
	for (int trial = 0; trial < 200; trial++) {
		iomoments_u64 hi = lcg_next(&state);
		iomoments_u64 lo = lcg_next(&state);
		s128_ref v = (s128_ref)(((u128_ref)hi << 64) | lo);
		double got = s128_to_double(s128_from_ref(v));
		double expected = (double)v;
		/* Relative tolerance 2^-52 (one ULP in double), plus a
		 * small absolute floor to handle values near zero. */
		double diff = fabs(got - expected);
		double scale = fabs(expected);
		if (scale > 1.0) {
			if (diff > scale * 2.220446049250313e-16) {
				FAIL_FMT("s128_to_double LCG trial %d: "
					 "got %g expected %g diff %g\n",
					 trial, got, expected, diff);
				return;
			}
		} else {
			if (diff > 1e-12) {
				FAIL_FMT("s128_to_double small trial %d: "
					 "got %g expected %g\n",
					 trial, got, expected);
				return;
			}
		}
	}
}

/* --- s128_mul_u64 ----------------------------------------------------- */

static void test_s128_mul_u64_boundaries(void)
{
	struct {
		s128_ref v;
		iomoments_u64 m;
	} cases[] = {
		{0, 0},
		{1, 0},
		{0, 0xDEADBEEFULL},
		{1, 1},
		{-1, 1},
		{1, 0xFFFFFFFFFFFFFFFFULL},
		{-1, 0xFFFFFFFFFFFFFFFFULL},
		/* s64 magnitude × u64: result is well within s128. */
		{(s128_ref)0x123456789ABCDEF0LL, 0x100ULL},
		{-(s128_ref)0x123456789ABCDEF0LL, 0x100ULL},
		/* Cross 64-bit boundary. */
		{(s128_ref)((u128_ref)1 << 70), 4},
		{-(s128_ref)((u128_ref)1 << 70), 4},
	};
	const size_t n = sizeof(cases) / sizeof(cases[0]);
	for (size_t i = 0; i < n; i++) {
		/* Reference: __int128 multiply, truncating to s128. */
		s128_ref expected = cases[i].v * (s128_ref)cases[i].m;
		struct s128 got =
			s128_mul_u64(s128_from_ref(cases[i].v), cases[i].m);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s128_mul_u64 case %zu m=%llu got hi=%llx "
				 "lo=%llx\n",
				 i, (unsigned long long)cases[i].m,
				 (unsigned long long)got.hi,
				 (unsigned long long)got.lo);
		}
	}
}

static void test_s128_mul_u64_lcg_sweep(void)
{
	iomoments_u64 state = 0x5A5A5A5AA5A5A5A5ULL;
	for (int trial = 0; trial < 2000; trial++) {
		iomoments_u64 v_hi = lcg_next(&state);
		iomoments_u64 v_lo = lcg_next(&state);
		iomoments_u64 m = lcg_next(&state);
		s128_ref v_ref = (s128_ref)(((u128_ref)v_hi << 64) | v_lo);
		s128_ref expected = v_ref * (s128_ref)m;
		struct s128 got = s128_mul_u64(s128_from_ref(v_ref), m);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s128_mul_u64 LCG trial %d\n", trial);
			return;
		}
	}
}

/* --- s128_mul_s64 ----------------------------------------------------- */

static void test_s128_mul_s64_boundaries(void)
{
	struct {
		s128_ref v;
		iomoments_s64 m;
	} cases[] = {
		{0, 0},
		{1, 0},
		{0, -1},
		{1, 1},
		{-1, 1},
		{1, -1},
		{-1, -1},
		/* Positive s128 × negative s64. */
		{(s128_ref)0x100, -2},
		/* Negative s128 × positive s64. */
		{-(s128_ref)0x100, 2},
		/* Negative s128 × negative s64 → positive. */
		{-(s128_ref)0x100, -2},
		/* INT64_MIN as multiplier. */
		{(s128_ref)1, (iomoments_s64)0x8000000000000000ULL},
		{-(s128_ref)1, (iomoments_s64)0x8000000000000000ULL},
		/* s128 straddling 64-bit boundary. */
		{(s128_ref)((u128_ref)1 << 70), -3},
		{-(s128_ref)((u128_ref)1 << 70), -3},
	};
	const size_t n = sizeof(cases) / sizeof(cases[0]);
	for (size_t i = 0; i < n; i++) {
		s128_ref expected = cases[i].v * (s128_ref)cases[i].m;
		struct s128 got =
			s128_mul_s64(s128_from_ref(cases[i].v), cases[i].m);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s128_mul_s64 case %zu m=%lld got hi=%llx "
				 "lo=%llx\n",
				 i, (long long)cases[i].m,
				 (unsigned long long)got.hi,
				 (unsigned long long)got.lo);
		}
	}
}

static void test_s128_mul_s64_lcg_sweep(void)
{
	iomoments_u64 state = 0x1357924680ABCDEFULL;
	for (int trial = 0; trial < 2000; trial++) {
		iomoments_u64 v_hi = lcg_next(&state);
		iomoments_u64 v_lo = lcg_next(&state);
		iomoments_s64 m = (iomoments_s64)lcg_next(&state);
		s128_ref v_ref = (s128_ref)(((u128_ref)v_hi << 64) | v_lo);
		s128_ref expected = v_ref * (s128_ref)m;
		struct s128 got = s128_mul_s64(s128_from_ref(v_ref), m);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s128_mul_s64 LCG trial %d\n", trial);
			return;
		}
	}
}

/* --- s128_div_u64 ----------------------------------------------------- */

static void test_s128_div_u64_boundaries(void)
{
	struct {
		s128_ref v;
		iomoments_u64 d;
	} cases[] = {
		{0, 1},
		{1, 1},
		{-1, 1},
		{42, 7},
		{-42, 7},
		{43, 7},  /* truncates toward zero: 43/7 = 6 */
		{-43, 7}, /* -43/7 = -6 (truncate toward zero, not -7) */
		{(s128_ref)1000000000000LL, 1000000ULL},
		{-(s128_ref)1000000000000LL, 1000000ULL},
		/* Numerator straddles the 64-bit half boundary. */
		{(s128_ref)((u128_ref)1 << 100), 7},
		{-(s128_ref)((u128_ref)1 << 100), 7},
		/* Numerator < divisor: quotient must be 0. */
		{5, 100},
		{-5, 100},
		/* Divisor large; quotient small. */
		{(s128_ref)((u128_ref)1 << 90), 0xFFFFFFFFFFFFFFFFULL},
	};
	const size_t n = sizeof(cases) / sizeof(cases[0]);
	for (size_t i = 0; i < n; i++) {
		s128_ref expected = cases[i].v / (s128_ref)cases[i].d;
		struct s128 got =
			s128_div_u64(s128_from_ref(cases[i].v), cases[i].d);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s128_div_u64 case %zu d=%llu got hi=%llx "
				 "lo=%llx\n",
				 i, (unsigned long long)cases[i].d,
				 (unsigned long long)got.hi,
				 (unsigned long long)got.lo);
		}
	}
}

static void test_s128_div_u64_lcg_sweep(void)
{
	iomoments_u64 state = 0xF00DBABECAFEFEEDULL;
	for (int trial = 0; trial < 2000; trial++) {
		iomoments_u64 v_hi = lcg_next(&state);
		iomoments_u64 v_lo = lcg_next(&state);
		iomoments_u64 d = lcg_next(&state);
		if (d == 0)
			d = 1; /* contract: d != 0 */
		s128_ref v_ref = (s128_ref)(((u128_ref)v_hi << 64) | v_lo);
		s128_ref expected = v_ref / (s128_ref)d;
		struct s128 got = s128_div_u64(s128_from_ref(v_ref), d);
		if (!s128_eq_ref(got, expected)) {
			FAIL_FMT("s128_div_u64 LCG trial %d\n", trial);
			return;
		}
	}
}

int main(void)
{
	test_u64_mul_u64_boundaries();
	test_u64_mul_u64_lcg_sweep();
	test_s64_mul_s64_boundaries();
	test_s64_mul_s64_lcg_sweep();
	test_s128_add_boundaries();
	test_s128_add_lcg_sweep();
	test_s128_sub_lcg_sweep();
	test_s128_from_s64();
	test_s128_to_double();
	test_s128_mul_u64_boundaries();
	test_s128_mul_u64_lcg_sweep();
	test_s128_mul_s64_boundaries();
	test_s128_mul_s64_lcg_sweep();
	test_s128_div_u64_boundaries();
	test_s128_div_u64_lcg_sweep();

	if (failures > 0) {
		fprintf(stderr, "\n%d assertion(s) failed.\n", failures);
		return 1;
	}
	printf("All u128.h primitive tests passed.\n");
	return 0;
}
