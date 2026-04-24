/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Behavioral tests for src/pebay.h at k=4.
 *
 * Fixtures are hand-computed — the whole point of testing this way is
 * that a reader can verify the expected values against the textbook
 * without running anything. Mean + variance fixtures are from
 * Wikipedia's "Standard deviation" article; skewness + kurtosis
 * fixtures are computed by hand and cross-checked against scipy
 * (`scipy.stats.moment` + `.skew` + `.kurtosis`).
 *
 * Exit code: 0 = all tests passed; 1 = at least one failed.
 */

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

#include "../../src/pebay.h"

static int failures;

#define CHECK(cond)                                                            \
	do {                                                                   \
		if (!(cond)) {                                                 \
			fprintf(stderr, "FAIL %s:%d  %s\n", __FILE__,          \
				__LINE__, #cond);                              \
			failures += 1;                                         \
		}                                                              \
	} while (0)

/*
 * Compare two doubles with a relative tolerance. Absolute tolerance
 * is used when `expected` is near zero so we don't divide by a tiny
 * number.
 */
static int approx_equal(double got, double expected, double rel_tol)
{
	double abs_tol = 1e-12;
	double diff = fabs(got - expected);
	double scale = fabs(expected);
	if (scale < abs_tol) {
		return diff <= abs_tol;
	}
	return diff <= rel_tol * scale;
}

/*
 * Fixture 1: mean([1, 2, 3]) == 2, variance == 2/3.
 *
 * Textbook: m1 = (1+2+3)/3 = 2; population variance =
 * ((1-2)² + (2-2)² + (3-2)²) / 3 = (1 + 0 + 1) / 3 = 2/3.
 */
static void test_tiny_stream(void)
{
	struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
	iomoments_summary_update(&s, 1.0);
	iomoments_summary_update(&s, 2.0);
	iomoments_summary_update(&s, 3.0);

	CHECK(s.n == 3);
	CHECK(approx_equal(iomoments_summary_mean(&s), 2.0, 1e-15));
	CHECK(approx_equal(iomoments_summary_variance(&s), 2.0 / 3.0, 1e-15));
}

/*
 * Fixture 2: textbook variance example, [2, 4, 4, 4, 5, 5, 7, 9].
 *
 * mean = (2+4+4+4+5+5+7+9)/8 = 40/8 = 5.
 * squared deviations = [9, 1, 1, 1, 0, 0, 4, 16], sum = 32.
 * population variance = 32/8 = 4.
 * (Same numbers appear in the Wikipedia "Standard deviation" article.)
 */
static void test_wikipedia_fixture(void)
{
	const double xs[] = {2, 4, 4, 4, 5, 5, 7, 9};
	const size_t n = sizeof(xs) / sizeof(xs[0]);
	struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < n; i++) {
		iomoments_summary_update(&s, xs[i]);
	}
	CHECK(s.n == n);
	CHECK(approx_equal(iomoments_summary_mean(&s), 5.0, 1e-15));
	CHECK(approx_equal(iomoments_summary_variance(&s), 4.0, 1e-15));
}

/*
 * Fixture 3: merge(stream_a, stream_b) equals stream_(a++b) within
 * relative tolerance. This is the property test that makes the
 * per-CPU accumulation design trustworthy — if merge is wrong, the
 * kernel-side output can't be aggregated to a single summary in
 * userspace without corrupting the result.
 *
 * Split point chosen so both halves are non-trivial and have
 * different means (otherwise the δ² correction term is zero and the
 * merge reduces to sum-of-m2, which doesn't exercise the
 * non-trivial path).
 */
static void test_merge_matches_single_stream(void)
{
	const double xs[] = {
		1.5, 2.0, 2.5, 3.0, 3.5, /* stream_a: mean 2.5 */
		7.0, 7.5, 8.0, 8.5	 /* stream_b: mean 7.75 */
	};
	const size_t split = 5;
	const size_t n = sizeof(xs) / sizeof(xs[0]);

	struct iomoments_summary full = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < n; i++) {
		iomoments_summary_update(&full, xs[i]);
	}

	struct iomoments_summary a = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary b = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < split; i++) {
		iomoments_summary_update(&a, xs[i]);
	}
	for (size_t i = split; i < n; i++) {
		iomoments_summary_update(&b, xs[i]);
	}
	iomoments_summary_merge(&a, &b);

	CHECK(a.n == full.n);
	CHECK(approx_equal(iomoments_summary_mean(&a),
			   iomoments_summary_mean(&full), 1e-12));
	CHECK(approx_equal(iomoments_summary_variance(&a),
			   iomoments_summary_variance(&full), 1e-12));
}

/*
 * Fixture 4: merging with an empty summary is the identity.
 * (Boundary case the merge function guards explicitly.)
 */
static void test_merge_with_empty(void)
{
	struct iomoments_summary populated = IOMOMENTS_SUMMARY_ZERO;
	iomoments_summary_update(&populated, 5.0);
	iomoments_summary_update(&populated, 7.0);

	struct iomoments_summary empty = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary target = populated;
	iomoments_summary_merge(&target, &empty);
	CHECK(target.n == populated.n);
	CHECK(approx_equal(target.m1, populated.m1, 1e-15));
	CHECK(approx_equal(target.m2, populated.m2, 1e-15));

	/* Reverse direction: empty + populated also equals populated. */
	struct iomoments_summary from_empty = IOMOMENTS_SUMMARY_ZERO;
	iomoments_summary_merge(&from_empty, &populated);
	CHECK(from_empty.n == populated.n);
	CHECK(approx_equal(from_empty.m1, populated.m1, 1e-15));
	CHECK(approx_equal(from_empty.m2, populated.m2, 1e-15));
}

/*
 * Fixture 5: self-merge aliasing safety. Semantically, observing
 * a stream twice doubles n and m2 while leaving m1 unchanged.
 * Without the b snapshot inside merge(), writes to `a` would clobber
 * the still-needed `b` reads (UB territory). The snapshot makes the
 * function aliasing-safe; this test pins that contract.
 */
static void test_self_merge_is_aliasing_safe(void)
{
	struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
	iomoments_summary_update(&s, 2.0);
	iomoments_summary_update(&s, 4.0);
	iomoments_summary_update(&s, 6.0);
	/* mean = 4.0, m2 = 8.0 (pop var 8/3 ≈ 2.6667), n = 3. */

	const uint64_t n_before = s.n;
	const double m1_before = s.m1;
	const double m2_before = s.m2;

	iomoments_summary_merge(&s, &s);

	CHECK(s.n == 2 * n_before);
	CHECK(approx_equal(s.m1, m1_before, 1e-15));
	CHECK(approx_equal(s.m2, 2.0 * m2_before, 1e-14));
}

/*
 * Fixture 6: getters return 0 on an empty summary (documented stable
 * contract). Also exercises iomoments_summary_init as the runtime-init
 * counterpart to IOMOMENTS_SUMMARY_ZERO — the two paths must leave the
 * summary in the same state across all four moments.
 */
static void test_empty_summary_readouts(void)
{
	struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
	CHECK(s.n == 0);
	CHECK(iomoments_summary_mean(&s) == 0.0);
	CHECK(iomoments_summary_variance(&s) == 0.0);
	CHECK(iomoments_summary_skewness(&s) == 0.0);
	CHECK(iomoments_summary_excess_kurtosis(&s) == 0.0);

	struct iomoments_summary s2;
	iomoments_summary_init(&s2);
	CHECK(s2.n == s.n);
	CHECK(s2.m1 == s.m1);
	CHECK(s2.m2 == s.m2);
	CHECK(s2.m3 == s.m3);
	CHECK(s2.m4 == s.m4);
}

/*
 * Fixture 7: hand-computed k=4 fixture on [1, 1, 1, 1, 2].
 *
 * Pick: small asymmetric sample with a non-trivial skew and a
 * non-Gaussian kurtosis, verifiable with grade-school arithmetic.
 *
 *   n        = 5
 *   mean     = (1+1+1+1+2)/5 = 6/5 = 1.2
 *   deviations d = [-0.2, -0.2, -0.2, -0.2, 0.8]
 *   M2       = 4·(0.04) + 0.64 = 0.16 + 0.64 = 0.80
 *   variance = M2/n = 0.80/5 = 0.16
 *   M3       = 4·(-0.008) + 0.512 = -0.032 + 0.512 = 0.48
 *   skewness = √n · M3 / M2^(3/2)
 *            = √5 · 0.48 / 0.80^1.5
 *            = 2.2360679... · 0.48 / 0.7155417528...
 *            = 1.5 exactly
 *   M4       = 4·(0.0016) + 0.4096 = 0.0064 + 0.4096 = 0.416
 *   excess κ = n · M4 / M2² - 3
 *            = 5 · 0.416 / 0.64 - 3
 *            = 3.25 - 3 = 0.25
 *
 * Cross-checked against scipy: scipy.stats.skew([1,1,1,1,2]) = 1.5;
 * scipy.stats.kurtosis([1,1,1,1,2]) = 0.25 (default fisher=True, i.e.
 * excess kurtosis).
 */
static void test_small_asymmetric_k4_fixture(void)
{
	const double xs[] = {1.0, 1.0, 1.0, 1.0, 2.0};
	const size_t n = sizeof(xs) / sizeof(xs[0]);
	struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < n; i++) {
		iomoments_summary_update(&s, xs[i]);
	}
	CHECK(s.n == n);
	CHECK(approx_equal(iomoments_summary_mean(&s), 1.2, 1e-14));
	CHECK(approx_equal(iomoments_summary_variance(&s), 0.16, 1e-14));
	CHECK(approx_equal(iomoments_summary_skewness(&s), 1.5, 1e-14));
	CHECK(approx_equal(iomoments_summary_excess_kurtosis(&s), 0.25, 1e-14));
}

/*
 * Fixture 8: symmetric sample [1, 2, 3, 4, 5]. Skewness is exactly
 * zero by symmetry; excess kurtosis is -1.3 (a platykurtic, flatter-
 * than-Gaussian tail — reasonable for a finite uniform-ish sample).
 *
 *   mean     = 3
 *   d        = [-2, -1, 0, 1, 2]
 *   M2       = 4 + 1 + 0 + 1 + 4 = 10
 *   variance = 10/5 = 2
 *   M3       = -8 + -1 + 0 + 1 + 8 = 0
 *   skewness = 0 (by symmetry)
 *   M4       = 16 + 1 + 0 + 1 + 16 = 34
 *   excess κ = 5·34/100 - 3 = 1.7 - 3 = -1.3
 *
 * Cross-checked against scipy: scipy.stats.skew([1,2,3,4,5]) = 0.0;
 * scipy.stats.kurtosis([1,2,3,4,5]) = -1.3.
 */
static void test_uniform_symmetric_fixture(void)
{
	const double xs[] = {1.0, 2.0, 3.0, 4.0, 5.0};
	const size_t n = sizeof(xs) / sizeof(xs[0]);
	struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < n; i++) {
		iomoments_summary_update(&s, xs[i]);
	}
	CHECK(s.n == n);
	CHECK(approx_equal(iomoments_summary_mean(&s), 3.0, 1e-15));
	CHECK(approx_equal(iomoments_summary_variance(&s), 2.0, 1e-15));
	CHECK(approx_equal(iomoments_summary_skewness(&s), 0.0, 1e-14));
	CHECK(approx_equal(iomoments_summary_excess_kurtosis(&s), -1.3, 1e-14));
}

/*
 * Fixture 9: k=4 parallel-combine round-trip. If merge(a, b) at k=4
 * doesn't match a single stream of the same samples for M3 and M4,
 * userspace can't reduce the per-CPU BPF maps into a single aggregate
 * skewness/kurtosis without corrupting the answer.
 *
 * Samples chosen so both halves have different means (otherwise the δ
 * correction terms vanish and the merge reduces to sum-of-Mₖ, which
 * doesn't exercise the non-trivial path).
 */
static void test_k4_merge_matches_single_stream(void)
{
	const double xs[] = {
		1.0, 1.0, 1.0, 1.0, /* stream_a: mean 1.0 */
		2.0, 3.0, 5.0, 8.0  /* stream_b: mean 4.5 */
	};
	const size_t split = 4;
	const size_t n = sizeof(xs) / sizeof(xs[0]);

	struct iomoments_summary full = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < n; i++) {
		iomoments_summary_update(&full, xs[i]);
	}

	struct iomoments_summary a = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary b = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < split; i++) {
		iomoments_summary_update(&a, xs[i]);
	}
	for (size_t i = split; i < n; i++) {
		iomoments_summary_update(&b, xs[i]);
	}
	iomoments_summary_merge(&a, &b);

	CHECK(a.n == full.n);
	CHECK(approx_equal(iomoments_summary_mean(&a),
			   iomoments_summary_mean(&full), 1e-13));
	CHECK(approx_equal(iomoments_summary_variance(&a),
			   iomoments_summary_variance(&full), 1e-13));
	CHECK(approx_equal(iomoments_summary_skewness(&a),
			   iomoments_summary_skewness(&full), 1e-12));
	CHECK(approx_equal(iomoments_summary_excess_kurtosis(&a),
			   iomoments_summary_excess_kurtosis(&full), 1e-12));
}

int main(void)
{
	test_tiny_stream();
	test_wikipedia_fixture();
	test_merge_matches_single_stream();
	test_merge_with_empty();
	test_self_merge_is_aliasing_safe();
	test_empty_summary_readouts();
	test_small_asymmetric_k4_fixture();
	test_uniform_symmetric_fixture();
	test_k4_merge_matches_single_stream();

	if (failures > 0) {
		fprintf(stderr, "\n%d assertion(s) failed.\n", failures);
		return 1;
	}
	printf("All Pébay k=4 tests passed.\n");
	return 0;
}
