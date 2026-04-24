/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Behavioral tests for src/pebay.h at k=2 (Welford).
 *
 * Fixtures are hand-computed — the whole point of testing the k=2 case
 * first is that a reader can verify the expected values against the
 * textbook without running anything. Extended with ULP-tolerance merge
 * property checks.
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
 * Fixture 6: variance getter returns 0 on an empty summary (documented
 * stable contract). Also exercises iomoments_summary_init as the
 * runtime-init counterpart to IOMOMENTS_SUMMARY_ZERO — the two paths
 * must leave the summary in the same state.
 */
static void test_empty_summary_readouts(void)
{
	struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
	CHECK(s.n == 0);
	CHECK(iomoments_summary_variance(&s) == 0.0);
	CHECK(iomoments_summary_mean(&s) == 0.0);

	struct iomoments_summary s2;
	iomoments_summary_init(&s2);
	CHECK(s2.n == s.n);
	CHECK(s2.m1 == s.m1);
	CHECK(s2.m2 == s.m2);
}

int main(void)
{
	test_tiny_stream();
	test_wikipedia_fixture();
	test_merge_matches_single_stream();
	test_merge_with_empty();
	test_self_merge_is_aliasing_safe();
	test_empty_summary_readouts();

	if (failures > 0) {
		fprintf(stderr, "\n%d assertion(s) failed.\n", failures);
		return 1;
	}
	printf("All Pébay k=2 tests passed.\n");
	return 0;
}
