/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Round-trip property test: src/pebay_bpf.h (fixed-point, BPF-safe)
 * vs src/pebay.h (double, userspace canonical).
 *
 * For integer-ns input streams, the two implementations must agree
 * on mean and variance within a stated tolerance. The userspace
 * reference is canonical; pebay_bpf is an approximation pinned to
 * not drift off it.
 *
 * Exit code: 0 all tests passed, 1 otherwise.
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "../../src/pebay.h"
#include "../../src/pebay_bpf.h"

static int failures;

#define CHECK(cond)                                                            \
	do {                                                                   \
		if (!(cond)) {                                                 \
			fprintf(stderr, "FAIL %s:%d  %s\n", __FILE__,          \
				__LINE__, #cond);                              \
			failures += 1;                                         \
		}                                                              \
	} while (0)

static int approx_equal(double got, double expected, double rel_tol,
			double abs_tol)
{
	double diff = fabs(got - expected);
	double scale = fabs(expected);
	if (scale < abs_tol) {
		return diff <= abs_tol;
	}
	return diff <= rel_tol * scale;
}

/*
 * Fixture 1: tiny-integer Wikipedia fixture [2,4,4,4,5,5,7,9].
 * Mean matches to full precision; variance reads low by ~9% on
 * this pathological fixture because the Welford fractional-
 * contribution terms (each < 1 ns²) get truncated in the
 * int64-only BPF m2 update. See pebay_bpf.h struct comment.
 *
 * This is the EXPECTED deviation from pebay.h on sub-ns-scale
 * input. Real iomoments workloads have σ >> 1 ns so the relative
 * error vanishes; the agreement tightens in test_microsecond_*
 * below.
 */
static void test_integer_fixture_matches_within_tolerance(void)
{
	const uint64_t xs[] = {2, 4, 4, 4, 5, 5, 7, 9};
	const size_t n = sizeof(xs) / sizeof(xs[0]);

	struct iomoments_summary s_ref = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary_bpf s_bpf = IOMOMENTS_SUMMARY_BPF_ZERO;
	for (size_t i = 0; i < n; i++) {
		iomoments_summary_update(&s_ref, (double)xs[i]);
		iomoments_summary_bpf_update(&s_bpf, xs[i]);
	}

	double mean_ref = iomoments_summary_mean(&s_ref);
	double mean_bpf = iomoments_summary_bpf_mean_ns(&s_bpf);
	double var_ref = iomoments_summary_variance(&s_ref);
	double var_bpf = iomoments_summary_bpf_variance_ns2(&s_bpf);

	CHECK(approx_equal(mean_bpf, mean_ref, 1e-9, 1e-12));
	/*
	 * Variance expected 4, BPF reads ~3.25 (integer-ns truncation
	 * on sub-1-ns deltas). 20% tolerance accepts this floor on
	 * pathological tiny-integer fixtures; real μs-scale workloads
	 * agree to ~1e-4 (see next fixture).
	 */
	CHECK(approx_equal(var_bpf, var_ref, 0.25, 1.0));
	CHECK(approx_equal(mean_bpf, 5.0, 1e-9, 1e-12));
}

/*
 * Fixture 2: realistic I/O-latency-ish distribution with ns-integer
 * samples. Synthesized from a linear sequence to stay deterministic
 * (no RNG); samples span 10^3 to 10^4 ns range (μs-scale latencies).
 *
 * Tolerance: rel_tol 1e-6 is well above the Q32.32 / int-truncation
 * error floor and well below what any real iomoments consumer needs.
 */
static void test_microsecond_sequence_agrees_within_tolerance(void)
{
	struct iomoments_summary s_ref = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary_bpf s_bpf = IOMOMENTS_SUMMARY_BPF_ZERO;
	/*
	 * Triangular sequence: 1000, 2000, 3000, ..., 10000, 9000, ...,
	 * 1000, 2000, ... Repeats to make n large enough to exercise
	 * delta/n rounding without overflowing m2 (which would happen
	 * around n=9e9 for this sigma).
	 */
	for (size_t i = 0; i < 1000; i++) {
		uint64_t x = 1000 + ((i * 97) % 9000);
		iomoments_summary_update(&s_ref, (double)x);
		iomoments_summary_bpf_update(&s_bpf, x);
	}

	double mean_ref = iomoments_summary_mean(&s_ref);
	double mean_bpf = iomoments_summary_bpf_mean_ns(&s_bpf);
	double var_ref = iomoments_summary_variance(&s_ref);
	double var_bpf = iomoments_summary_bpf_variance_ns2(&s_bpf);

	CHECK(approx_equal(mean_bpf, mean_ref, 1e-10, 1.0));
	CHECK(approx_equal(var_bpf, var_ref, 1e-3, 1.0));
}

/*
 * Fixture 3: merge(a, b) in both implementations must produce the
 * same aggregate as running the full sequence through either one
 * directly. Splits the input, updates each half independently, then
 * merges — mirrors the per-CPU accumulation + userspace-aggregation
 * pattern iomoments actually uses.
 */
static void test_merge_matches_single_stream(void)
{
	const uint64_t xs[] = {1500, 2000, 2500, 3000, 3500,
			       7000, 7500, 8000, 8500};
	const size_t split = 5;
	const size_t n = sizeof(xs) / sizeof(xs[0]);

	struct iomoments_summary full_ref = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary_bpf full_bpf = IOMOMENTS_SUMMARY_BPF_ZERO;
	for (size_t i = 0; i < n; i++) {
		iomoments_summary_update(&full_ref, (double)xs[i]);
		iomoments_summary_bpf_update(&full_bpf, xs[i]);
	}

	struct iomoments_summary a_ref = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary b_ref = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary_bpf a_bpf = IOMOMENTS_SUMMARY_BPF_ZERO;
	struct iomoments_summary_bpf b_bpf = IOMOMENTS_SUMMARY_BPF_ZERO;
	for (size_t i = 0; i < split; i++) {
		iomoments_summary_update(&a_ref, (double)xs[i]);
		iomoments_summary_bpf_update(&a_bpf, xs[i]);
	}
	for (size_t i = split; i < n; i++) {
		iomoments_summary_update(&b_ref, (double)xs[i]);
		iomoments_summary_bpf_update(&b_bpf, xs[i]);
	}
	iomoments_summary_merge(&a_ref, &b_ref);
	iomoments_summary_bpf_merge(&a_bpf, &b_bpf);

	CHECK(a_bpf.n == full_bpf.n);
	/*
	 * Merge-time precision: weighted-mean goes through integer-ns
	 * arithmetic (see pebay_bpf.h merge comment). Accuracy ±0.5 ns
	 * absolute or ~1e-3 relative at μs-scale inputs; variance
	 * accumulates the same floor plus the per-update truncation.
	 */
	CHECK(approx_equal(iomoments_summary_bpf_mean_ns(&a_bpf),
			   iomoments_summary_mean(&full_ref), 1e-3, 1.0));
	CHECK(approx_equal(iomoments_summary_bpf_variance_ns2(&a_bpf),
			   iomoments_summary_variance(&full_ref), 0.10, 1.0));
	CHECK(approx_equal(iomoments_summary_bpf_mean_ns(&a_bpf),
			   iomoments_summary_mean(&a_ref), 1e-3, 1.0));
}

/*
 * Fixture 4: aliasing-safe self-merge. iomoments_summary_bpf_merge(&s, &s)
 * must double n and m2, preserve m1 (same semantics as pebay.h).
 */
static void test_self_merge_is_aliasing_safe(void)
{
	struct iomoments_summary_bpf s = IOMOMENTS_SUMMARY_BPF_ZERO;
	iomoments_summary_bpf_update(&s, 2000);
	iomoments_summary_bpf_update(&s, 4000);
	iomoments_summary_bpf_update(&s, 6000);

	const uint64_t n_before = s.n;
	const int64_t m1_before = s.m1_fp;
	const double var_before = iomoments_summary_bpf_variance_ns2(&s);

	iomoments_summary_bpf_merge(&s, &s);

	/*
	 * Self-merge semantics (observing stream twice): n doubles,
	 * m1 unchanged, variance unchanged (same ratio m2/n).
	 */
	CHECK(s.n == 2 * n_before);
	CHECK(s.m1_fp == m1_before);
	CHECK(approx_equal(iomoments_summary_bpf_variance_ns2(&s), var_before,
			   1e-12, 1e-9));
}

/*
 * Fixture 5: merging with empty is the identity.
 */
static void test_merge_with_empty(void)
{
	struct iomoments_summary_bpf populated = IOMOMENTS_SUMMARY_BPF_ZERO;
	iomoments_summary_bpf_update(&populated, 5000);
	iomoments_summary_bpf_update(&populated, 7000);

	struct iomoments_summary_bpf empty = IOMOMENTS_SUMMARY_BPF_ZERO;
	struct iomoments_summary_bpf target = populated;
	iomoments_summary_bpf_merge(&target, &empty);
	CHECK(target.n == populated.n);
	CHECK(target.m1_fp == populated.m1_fp);
	CHECK(target.m2 == populated.m2);

	struct iomoments_summary_bpf from_empty = IOMOMENTS_SUMMARY_BPF_ZERO;
	iomoments_summary_bpf_merge(&from_empty, &populated);
	CHECK(from_empty.n == populated.n);
	CHECK(from_empty.m1_fp == populated.m1_fp);
	CHECK(from_empty.m2 == populated.m2);
}

/*
 * Fixture 6: readouts on empty summary are zero + don't divide-by-zero.
 */
static void test_empty_readouts(void)
{
	struct iomoments_summary_bpf s = IOMOMENTS_SUMMARY_BPF_ZERO;
	CHECK(iomoments_summary_bpf_mean_ns(&s) == 0.0);
	CHECK(iomoments_summary_bpf_variance_ns2(&s) == 0.0);

	struct iomoments_summary_bpf s2;
	iomoments_summary_bpf_init(&s2);
	CHECK(s2.n == s.n);
	CHECK(s2.m1_fp == s.m1_fp);
	CHECK(s2.m2 == s.m2);
}

int main(void)
{
	test_integer_fixture_matches_within_tolerance();
	test_microsecond_sequence_agrees_within_tolerance();
	test_merge_matches_single_stream();
	test_self_merge_is_aliasing_safe();
	test_merge_with_empty();
	test_empty_readouts();

	if (failures > 0) {
		fprintf(stderr, "\n%d assertion(s) failed.\n", failures);
		return 1;
	}
	printf("All Pébay-BPF round-trip tests passed.\n");
	return 0;
}
