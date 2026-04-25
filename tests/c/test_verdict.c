/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Tests for src/iomoments_verdict.h — D007 verdict-emission.
 *
 * Each fixture constructs a global summary + window ring + Level 2
 * result that should produce a specific verdict status, and checks
 * that the verdict emitted matches expectation.
 *
 * The strategy is to test the *individual signal evaluators* for
 * specific status outputs on synthetic inputs, plus the worst-of-all
 * aggregation. Direction and threshold-band assertions, not
 * exact-rationale-string match (rationale text may evolve).
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../src/iomoments_level2.h"
#include "../../src/iomoments_verdict.h"
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

/* Build a global summary by feeding N samples ~ N(μ, σ²) via Pébay. */
static void build_normal_global(struct iomoments_summary *out, size_t n,
				double mu, double sigma)
{
	*out = (struct iomoments_summary)IOMOMENTS_SUMMARY_ZERO;
	uint64_t state = 0xCAFEDEADBEEFULL;
	int has_cached = 0;
	double cached = 0.0;
	for (size_t i = 0; i < n; i++) {
		double sample;
		if (has_cached) {
			sample = cached;
			has_cached = 0;
		} else {
			state = state * 6364136223846793005ULL +
				1442695040888963407ULL;
			double u1 =
				(double)(state >> 11) / (double)(1ULL << 53);
			state = state * 6364136223846793005ULL +
				1442695040888963407ULL;
			double u2 =
				(double)(state >> 11) / (double)(1ULL << 53);
			if (u1 <= 0.0)
				u1 = 1e-12;
			double r = sqrt(-2.0 * log(u1));
			double t = 2.0 * 3.14159265358979323846 * u2;
			sample = r * cos(t);
			cached = r * sin(t);
			has_cached = 1;
		}
		iomoments_summary_update(out, mu + sigma * sample);
	}
}

/* --- Status-name helper test ------------------------------------------ */

static void test_status_names(void)
{
	CHECK(strcmp(iomoments_verdict_status_name(IOMOMENTS_VERDICT_GREEN),
		     "GREEN") == 0);
	CHECK(strcmp(iomoments_verdict_status_name(IOMOMENTS_VERDICT_YELLOW),
		     "YELLOW") == 0);
	CHECK(strcmp(iomoments_verdict_status_name(IOMOMENTS_VERDICT_AMBER),
		     "AMBER") == 0);
	CHECK(strcmp(iomoments_verdict_status_name(IOMOMENTS_VERDICT_RED),
		     "RED") == 0);
}

/* --- Sample-count signal --------------------------------------------- */

static void test_sample_count_red_when_too_few(void)
{
	struct iomoments_summary g;
	build_normal_global(&g, 50, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_sample_count(&g, &v);
	CHECK(v.n_signals == 1);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_RED);
	CHECK(v.overall == IOMOMENTS_VERDICT_RED);
}

static void test_sample_count_yellow_in_band(void)
{
	struct iomoments_summary g;
	build_normal_global(&g, 500, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_sample_count(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_YELLOW);
}

static void test_sample_count_green_above_threshold(void)
{
	struct iomoments_summary g;
	build_normal_global(&g, 5000, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_sample_count(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

/* --- Variance-sanity signal ------------------------------------------ */

static void test_variance_sanity_red_on_constant_stream(void)
{
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 1000; i++) {
		iomoments_summary_update(&g, 42.0);
	}
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_variance_sanity(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_RED);
}

static void test_variance_sanity_green_on_normal(void)
{
	struct iomoments_summary g;
	build_normal_global(&g, 1000, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_variance_sanity(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

/* --- Kurtosis-sanity signal ------------------------------------------ */

static void test_kurtosis_sanity_green_on_normal(void)
{
	/* Gaussian has excess kurtosis = 0; with finite n the estimator
	 * is noisy but should land near 0. */
	struct iomoments_summary g;
	build_normal_global(&g, 5000, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_kurtosis_sanity(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_kurtosis_sanity_red_on_degenerate_spike(void)
{
	/* 999 samples at 1000ns + 1 outlier at 100000 → very heavy
	 * kurtosis, well above the RED threshold of 50. */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 999; i++) {
		iomoments_summary_update(&g, 1000.0);
	}
	iomoments_summary_update(&g, 100000.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_kurtosis_sanity(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_RED);
}

/* --- Carleman signal -------------------------------------------------- */

static void test_carleman_green_on_gaussian(void)
{
	/* Gaussian central moments: μ_2 = σ², μ_4 = 3σ⁴.
	 * term1 = σ^(-1), term2 = (3σ⁴)^(-1/4) = σ^(-1) · 3^(-1/4)
	 * ratio = 3^(-1/4) ≈ 0.7598. Comfortably > 0.5 → GREEN. */
	struct iomoments_summary g;
	build_normal_global(&g, 5000, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_carleman(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_carleman_amber_on_heavy_tail_spike(void)
{
	/* 999 samples at 1000 + 1 spike at 100000. m_4 dominated by the
	 * spike, m_2 dominated less so → ratio collapses → AMBER. */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 999; i++) {
		iomoments_summary_update(&g, 1000.0);
	}
	iomoments_summary_update(&g, 100000.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_carleman(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_carleman_yellow_on_constant_stream(void)
{
	/* m_2 = m_4 = 0 → cannot evaluate → YELLOW (not RED — that's
	 * variance_sanity's job). */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 1000; i++) {
		iomoments_summary_update(&g, 42.0);
	}
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_carleman(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_YELLOW);
}

/* --- Nyquist + autocorr signals (synthetic Level-2 results) ----------- */

static void test_nyquist_green_on_high_confidence(void)
{
	struct iomoments_level2_result l2;
	memset(&l2, 0, sizeof(l2));
	l2.n_windows = 100;
	l2.nyquist_confidence = 0.85;
	l2.variance_ratio = 1.1;
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_nyquist(&l2, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_nyquist_amber_on_low_confidence(void)
{
	struct iomoments_level2_result l2;
	memset(&l2, 0, sizeof(l2));
	l2.n_windows = 100;
	l2.nyquist_confidence = 0.05;
	l2.variance_ratio = 0.1;
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_nyquist(&l2, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_autocorr_amber_on_strong_periodicity(void)
{
	struct iomoments_level2_result l2;
	memset(&l2, 0, sizeof(l2));
	l2.n_windows = 100;
	l2.autocorr[0] = 0.1;
	l2.autocorr[1] = 0.7; /* lag 2: peak */
	l2.autocorr[2] = 0.1;
	l2.autocorr[3] = 0.05;
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_autocorr(&l2, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_autocorr_green_on_low_correlation(void)
{
	struct iomoments_level2_result l2;
	memset(&l2, 0, sizeof(l2));
	l2.n_windows = 100;
	l2.autocorr[0] = 0.05;
	l2.autocorr[1] = -0.08;
	l2.autocorr[2] = 0.02;
	l2.autocorr[3] = -0.10;
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_autocorr(&l2, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

/* --- Half-split stability signal ------------------------------------- */

static void build_window_ring_stationary(struct iomoments_window *ring,
					 size_t n_windows, size_t k_per_window,
					 double mu, double sigma, uint64_t seed)
{
	uint64_t state = seed;
	int has_cached = 0;
	double cached = 0.0;
	for (size_t i = 0; i < n_windows; i++) {
		struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
		for (size_t j = 0; j < k_per_window; j++) {
			double sample;
			if (has_cached) {
				sample = cached;
				has_cached = 0;
			} else {
				state = state * 6364136223846793005ULL +
					1442695040888963407ULL;
				double u1 = (double)(state >> 11) /
					    (double)(1ULL << 53);
				state = state * 6364136223846793005ULL +
					1442695040888963407ULL;
				double u2 = (double)(state >> 11) /
					    (double)(1ULL << 53);
				if (u1 <= 0.0)
					u1 = 1e-12;
				double r = sqrt(-2.0 * log(u1));
				double t = 2.0 * 3.14159265358979323846 * u2;
				sample = r * cos(t);
				cached = r * sin(t);
				has_cached = 1;
			}
			iomoments_summary_update(&s, mu + sigma * sample);
		}
		ring[i].end_ts_ns = i * 100000000ULL;
		ring[i].summary = s;
	}
}

static void test_half_split_green_on_stationary(void)
{
	struct iomoments_window ring[64];
	build_window_ring_stationary(ring, 64, 200, 1000.0, 50.0,
				     0xC0FFEE0001ULL);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_half_split(ring, 64, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_half_split_amber_on_drift(void)
{
	/* Build 64 windows: first half mean 1000, second half mean 1500. */
	struct iomoments_window ring[64];
	build_window_ring_stationary(ring, 32, 200, 1000.0, 50.0,
				     0xDEADBEEFULL);
	build_window_ring_stationary(ring + 32, 32, 200, 1500.0, 50.0,
				     0xFEEDFACEULL);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_half_split(ring, 64, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_half_split_yellow_when_too_few(void)
{
	struct iomoments_window ring[5];
	build_window_ring_stationary(ring, 5, 200, 1000.0, 50.0, 0xABCDULL);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_half_split(ring, 5, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_YELLOW);
}

/* --- Worst-of-all aggregation ----------------------------------------- */

static void test_overall_is_worst_of_all(void)
{
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_push(&v, "a", IOMOMENTS_VERDICT_GREEN, "ok");
	CHECK(v.overall == IOMOMENTS_VERDICT_GREEN);
	iomoments_verdict_push(&v, "b", IOMOMENTS_VERDICT_YELLOW, "mild");
	CHECK(v.overall == IOMOMENTS_VERDICT_YELLOW);
	iomoments_verdict_push(&v, "c", IOMOMENTS_VERDICT_AMBER, "stronger");
	CHECK(v.overall == IOMOMENTS_VERDICT_AMBER);
	iomoments_verdict_push(&v, "d", IOMOMENTS_VERDICT_GREEN, "ok again");
	CHECK(v.overall == IOMOMENTS_VERDICT_AMBER); /* doesn't decrease */
	iomoments_verdict_push(&v, "e", IOMOMENTS_VERDICT_RED, "fatal");
	CHECK(v.overall == IOMOMENTS_VERDICT_RED);
}

/* --- End-to-end on a clean stationary scenario ------------------------ */

static void test_e2e_stationary_should_be_green(void)
{
	struct iomoments_window ring[64];
	build_window_ring_stationary(ring, 64, 200, 1000.0, 50.0,
				     0x123456789ABCDULL);
	struct iomoments_summary global = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < 64; i++) {
		iomoments_summary_merge(&global, &ring[i].summary);
	}
	struct iomoments_level2_result l2;
	iomoments_level2_analyze(ring, 64, &global, &l2);
	struct iomoments_verdict v;
	iomoments_verdict_compute(&global, ring, 64, &l2, &v);
	CHECK(v.overall == IOMOMENTS_VERDICT_GREEN);
}

int main(void)
{
	test_status_names();
	test_sample_count_red_when_too_few();
	test_sample_count_yellow_in_band();
	test_sample_count_green_above_threshold();
	test_variance_sanity_red_on_constant_stream();
	test_variance_sanity_green_on_normal();
	test_kurtosis_sanity_green_on_normal();
	test_kurtosis_sanity_red_on_degenerate_spike();
	test_carleman_green_on_gaussian();
	test_carleman_amber_on_heavy_tail_spike();
	test_carleman_yellow_on_constant_stream();
	test_nyquist_green_on_high_confidence();
	test_nyquist_amber_on_low_confidence();
	test_autocorr_amber_on_strong_periodicity();
	test_autocorr_green_on_low_correlation();
	test_half_split_green_on_stationary();
	test_half_split_amber_on_drift();
	test_half_split_yellow_when_too_few();
	test_overall_is_worst_of_all();
	test_e2e_stationary_should_be_green();

	if (failures > 0) {
		fprintf(stderr, "\n%d assertion(s) failed.\n", failures);
		return 1;
	}
	printf("All D007 verdict tests passed.\n");
	return 0;
}
