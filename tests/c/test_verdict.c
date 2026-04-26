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
#include "../../src/iomoments_spectral.h"
#include "../../src/iomoments_topk.h"
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
			/* (state >> 11) + 1 maps u1 to (0, 1] with no
			 * zero — log(u1) is well-defined for every state
			 * without the post-hoc 1e-12 clamp's small bias. */
			double u1 = (double)((state >> 11) + 1ULL) /
				    (double)(1ULL << 53);
			state = state * 6364136223846793005ULL +
				1442695040888963407ULL;
			double u2 =
				(double)(state >> 11) / (double)(1ULL << 53);
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

/* --- Hankel signal --------------------------------------------------- */

static void test_hankel_green_on_gaussian(void)
{
	/* Gaussian: μ_2 = σ², μ_3 = 0, μ_4 = 3σ⁴.
	 * det(H₃) = σ²·3σ⁴ − 0 − σ⁶ = 2σ⁶
	 * κ = det/μ_2³ = 2σ⁶/σ⁶ = 2 → GREEN. */
	struct iomoments_summary g;
	build_normal_global(&g, 5000, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_hankel(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_hankel_amber_on_two_atom_distribution(void)
{
	/* Symmetric two-atom ±1: μ_2 = 1, μ_3 = 0, μ_4 = 1.
	 * det(H₃) = 1·1 − 0 − 1 = 0 → κ = 0 → AMBER. */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 500; i++) {
		iomoments_summary_update(&g, -1.0);
		iomoments_summary_update(&g, +1.0);
	}
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_hankel(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_hankel_amber_on_heavy_tail_spike(void)
{
	/* 999 at 1000ns + 1 at 100000ns: effectively two atoms,
	 * Hankel rank-deficient → κ near 0 → AMBER. */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 999; i++) {
		iomoments_summary_update(&g, 1000.0);
	}
	iomoments_summary_update(&g, 100000.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_hankel(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_hankel_yellow_on_constant_stream(void)
{
	/* m_2 = 0 → cannot evaluate → YELLOW (variance_sanity catches
	 * this case as RED separately). */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 1000; i++) {
		iomoments_summary_update(&g, 42.0);
	}
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_hankel(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_YELLOW);
}

/* --- Jarque-Bera signal ---------------------------------------------- */

static void test_jb_green_on_gaussian(void)
{
	/* Gaussian: skewness ≈ 0, excess kurtosis ≈ 0. JB statistic ≈ 0,
	 * χ²(2) p-value ≈ 1 → comfortably non-rejection of normality. */
	struct iomoments_summary g;
	build_normal_global(&g, 5000, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_jb(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_jb_amber_on_strong_skew(void)
{
	/* Mostly small + a single extreme outlier → very large skew &
	 * kurtosis → JB statistic blows up → p-value vanishes →
	 * AMBER. */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 999; i++) {
		iomoments_summary_update(&g, 1000.0 + (i % 7));
	}
	iomoments_summary_update(&g, 100000.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_jb(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_jb_yellow_on_low_n(void)
{
	/* Below the JB asymptotic-validity threshold → YELLOW with
	 * insufficient-data rationale (don't claim non-normality on
	 * 10 samples). */
	struct iomoments_summary g;
	build_normal_global(&g, 6, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_jb(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_YELLOW);
}

/* --- Edgeworth-residual signal --------------------------------------- */

static void test_edgeworth_green_on_gaussian(void)
{
	/* Gaussian: skewness ≈ 0, excess kurtosis ≈ 0 → truncated
	 * Edgeworth correction factor is ≈ 1 everywhere → PDF stays
	 * positive across the grid → GREEN. */
	struct iomoments_summary g;
	build_normal_global(&g, 5000, 1000.0, 50.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_edgeworth(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_edgeworth_amber_on_extreme_kurtosis(void)
{
	/* Spike fixture: large excess kurtosis → Hermite-polynomial
	 * correction overshoots negative on the tails → truncated PDF
	 * goes negative → AMBER (moment-implied PDF is not a valid
	 * density). */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 999; i++) {
		iomoments_summary_update(&g, 1000.0 + (i % 5));
	}
	iomoments_summary_update(&g, 100000.0);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_edgeworth(&g, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_edgeworth_yellow_on_constant_stream(void)
{
	/* m_2 = 0 → cannot standardize → YELLOW (variance_sanity covers
	 * the RED case separately). */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 1000; i++) {
		iomoments_summary_update(&g, 42.0);
	}
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_edgeworth(&g, &v);
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

/* --- Hill tail-index signal ------------------------------------------ */

static double pareto_sample_in_test(double alpha, uint64_t *state)
{
	*state = (*state) * 6364136223846793005ULL + 1442695040888963407ULL;
	uint64_t u53 = *state >> 11;
	double u = (double)u53 / (double)(1ULL << 53);
	if (u <= 0.0)
		u = 1e-12;
	return pow(u, -1.0 / alpha);
}

static void fill_pareto_topk(struct iomoments_topk *t, double alpha, size_t n,
			     uint64_t seed)
{
	uint64_t state = seed;
	for (size_t i = 0; i < n; i++) {
		double s = pareto_sample_in_test(alpha, &state);
		uint64_t q = (uint64_t)(s * 1e6);
		if (q == 0)
			q = 1;
		iomoments_topk_insert(t, q);
	}
}

static void test_hill_green_on_light_tail(void)
{
	/* Pareto(α=3.5) → estimator should land > 2.5 → GREEN. */
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	fill_pareto_topk(&t, 3.5, 10000, 0xCAFE001ULL);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_hill(&t, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_hill_amber_on_heavy_tail(void)
{
	/* Pareto(α=1.5) → estimator should land in (1, 2] → AMBER. */
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	fill_pareto_topk(&t, 1.5, 10000, 0xBEEF002ULL);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_hill(&t, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_hill_red_on_very_heavy_tail(void)
{
	/* Pareto(α=0.6) → estimator should land ≤ 1 → RED.
	 * Mean doesn't exist for stationary measure → moments are
	 * the wrong primitive. */
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	fill_pareto_topk(&t, 0.6, 10000, 0xF00D003ULL);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_hill(&t, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_RED);
}

static void test_hill_yellow_on_empty_topk(void)
{
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_hill(&t, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_YELLOW);
}

/* --- Spectral-flatness sweep + evaluator ------------------------------ */

/* Forward decl — used by spectral fixtures; full definition is below
 * with the half-split tests. */
static void build_window_ring_stationary(struct iomoments_window *ring,
					 size_t n_windows, size_t k_per_window,
					 double mu, double sigma,
					 uint64_t seed);

/*
 * Synthesize windows where each window's m1 is sampled from a pure
 * sinusoid in window-index space. period_in_windows controls the
 * hidden period; amplitude controls the strength. n samples per
 * window contribute to m2 (white noise around the sinusoid).
 */
static void build_window_ring_sinusoidal_mean(struct iomoments_window *ring,
					      size_t n_windows,
					      size_t k_per_window, double mu,
					      double amplitude,
					      double period_in_windows,
					      double sigma, uint64_t seed)
{
	uint64_t state = seed;
	int has_cached = 0;
	double cached = 0.0;
	for (size_t i = 0; i < n_windows; i++) {
		double window_mu =
			mu + amplitude * sin(2.0 * 3.14159265358979323846 *
					     (double)i / period_in_windows);
		struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
		for (size_t j = 0; j < k_per_window; j++) {
			double sample;
			if (has_cached) {
				sample = cached;
				has_cached = 0;
			} else {
				state = state * 6364136223846793005ULL +
					1442695040888963407ULL;
				/* (state >> 11) + 1 → u1 ∈ (0, 1]; same
				 * unbias as build_normal_global. */
				double u1 = (double)((state >> 11) + 1ULL) /
					    (double)(1ULL << 53);
				state = state * 6364136223846793005ULL +
					1442695040888963407ULL;
				double u2 = (double)(state >> 11) /
					    (double)(1ULL << 53);
				double r = sqrt(-2.0 * log(u1));
				double t = 2.0 * 3.14159265358979323846 * u2;
				sample = r * cos(t);
				cached = r * sin(t);
				has_cached = 1;
			}
			iomoments_summary_update(&s,
						 window_mu + sigma * sample);
		}
		ring[i].end_ts_ns = i * 100000000ULL;
		ring[i].summary = s;
		iomoments_topk_init(&ring[i].topk);
	}
}

static void test_spectral_sweep_stationary_smooth(void)
{
	/* 128 stationary windows. Sweep should produce ratios near 1
	 * across all k → min_ratio > 0.5 → GREEN. */
	struct iomoments_window ring[128];
	build_window_ring_stationary(ring, 128, 200, 1000.0, 50.0,
				     0xCAFE0001ULL);
	struct iomoments_summary global = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < 128; i++) {
		iomoments_summary_merge(&global, &ring[i].summary);
	}
	struct iomoments_spectral_result spec;
	iomoments_spectral_sweep(ring, 128, &global, 0.1, &spec);
	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_spectral(&spec, &v);
	CHECK(!spec.insufficient_data);
	CHECK(spec.n_points >= 3);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_GREEN);
}

static void test_spectral_sweep_sinusoid_at_period_4(void)
{
	/* 128 windows with hidden sinusoid of period 4 windows.
	 * The sweep at k=4 averages exactly one period, so var_obs
	 * collapses far below CLT prediction → low min_ratio,
	 * AMBER. The min_ratio's W' should be 4 · base = 0.4 s. */
	struct iomoments_window ring[128];
	build_window_ring_sinusoidal_mean(ring, 128, 200, 1000.0,
					  300.0, /* large amplitude */
					  4.0,	 /* period 4 windows */
					  20.0,	 /* small noise */
					  0xBEEF0001ULL);
	struct iomoments_summary global = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < 128; i++) {
		iomoments_summary_merge(&global, &ring[i].summary);
	}
	struct iomoments_spectral_result spec;
	iomoments_spectral_sweep(ring, 128, &global, 0.1, &spec);
	CHECK(!spec.insufficient_data);
	CHECK(spec.min_ratio < 0.2);
	CHECK(spec.min_ratio_idx < spec.n_points);
	/*
	 * The dip occurs at every k that's a multiple of the period
	 * (k=4, 8, 16, ...) since each averages an integer number of
	 * periods. With noise, numerical precision determines which
	 * k holds the global minimum. Just check the localized W' is
	 * a multiple of the true period (4 base windows = 0.4 s).
	 */
	double min_w = spec.points[spec.min_ratio_idx].window_seconds;
	double frac = min_w / 0.4;
	CHECK(fabs(frac - round(frac)) < 1e-9);

	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_spectral(&spec, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_AMBER);
}

static void test_spectral_sweep_insufficient_data(void)
{
	struct iomoments_window ring[3];
	build_window_ring_stationary(ring, 3, 200, 1000.0, 50.0, 0x123ULL);
	struct iomoments_summary global = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < 3; i++) {
		iomoments_summary_merge(&global, &ring[i].summary);
	}
	struct iomoments_spectral_result spec;
	iomoments_spectral_sweep(ring, 3, &global, 0.1, &spec);
	CHECK(spec.insufficient_data == 1);

	struct iomoments_verdict v;
	memset(&v, 0, sizeof(v));
	iomoments_verdict_eval_spectral(&spec, &v);
	CHECK(v.signals[0].status == IOMOMENTS_VERDICT_YELLOW);
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
		struct iomoments_topk t;
		iomoments_topk_init(&t);
		for (size_t j = 0; j < k_per_window; j++) {
			double sample;
			if (has_cached) {
				sample = cached;
				has_cached = 0;
			} else {
				state = state * 6364136223846793005ULL +
					1442695040888963407ULL;
				/* (state >> 11) + 1 → u1 ∈ (0, 1]; same
				 * unbias as build_normal_global. */
				double u1 = (double)((state >> 11) + 1ULL) /
					    (double)(1ULL << 53);
				state = state * 6364136223846793005ULL +
					1442695040888963407ULL;
				double u2 = (double)(state >> 11) /
					    (double)(1ULL << 53);
				double r = sqrt(-2.0 * log(u1));
				double t_phase =
					2.0 * 3.14159265358979323846 * u2;
				sample = r * cos(t_phase);
				cached = r * sin(t_phase);
				has_cached = 1;
			}
			double x = mu + sigma * sample;
			iomoments_summary_update(&s, x);
			if (x > 0.0) {
				iomoments_topk_insert(&t, (iomoments_u64)x);
			}
		}
		ring[i].end_ts_ns = i * 100000000ULL;
		ring[i].summary = s;
		ring[i].topk = t;
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

/*
 * Verifies the red_verdict_refuses_moment_emission constraint
 * precondition: when the data triggers any RED signal,
 * iomoments_verdict_compute promotes the overall to RED. The
 * print_verdict path's "Moment-based summary refused" block fires
 * on overall == RED — that's a userspace stdout effect not unit-
 * testable here, but the precondition (overall reaches RED) is
 * pinned by this test.
 */
static void test_red_overall_propagates_from_constant_stream(void)
{
	/* Constant stream of 1000 samples → variance_sanity = RED →
	 * overall = RED. Build a window ring of 16 windows so
	 * half_split has enough material to evaluate (rather than
	 * returning YELLOW for too-few-windows). */
	struct iomoments_summary g = IOMOMENTS_SUMMARY_ZERO;
	for (int i = 0; i < 1000; i++) {
		iomoments_summary_update(&g, 42.0);
	}
	struct iomoments_window ring[16];
	memset(ring, 0, sizeof(ring));
	for (size_t i = 0; i < 16; i++) {
		ring[i].end_ts_ns = i * 100000000ULL;
		ring[i].summary =
			(struct iomoments_summary)IOMOMENTS_SUMMARY_ZERO;
		iomoments_topk_init(&ring[i].topk);
		for (int j = 0; j < 100; j++) {
			iomoments_summary_update(&ring[i].summary, 42.0);
		}
	}
	struct iomoments_level2_result l2;
	iomoments_level2_analyze(ring, 16, &g, &l2);
	struct iomoments_spectral_result spec;
	iomoments_spectral_sweep(ring, 16, &g, 0.1, &spec);
	struct iomoments_verdict v;
	iomoments_verdict_compute(&g, ring, 16, &l2, &spec, &v);
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
	struct iomoments_spectral_result spec;
	iomoments_spectral_sweep(ring, 64, &global, 0.1, &spec);
	iomoments_verdict_compute(&global, ring, 64, &l2, &spec, &v);
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
	test_hill_green_on_light_tail();
	test_hill_amber_on_heavy_tail();
	test_hill_red_on_very_heavy_tail();
	test_hill_yellow_on_empty_topk();
	test_hankel_green_on_gaussian();
	test_hankel_amber_on_two_atom_distribution();
	test_hankel_amber_on_heavy_tail_spike();
	test_hankel_yellow_on_constant_stream();
	test_jb_green_on_gaussian();
	test_jb_amber_on_strong_skew();
	test_jb_yellow_on_low_n();
	test_edgeworth_green_on_gaussian();
	test_edgeworth_amber_on_extreme_kurtosis();
	test_edgeworth_yellow_on_constant_stream();
	test_nyquist_green_on_high_confidence();
	test_nyquist_amber_on_low_confidence();
	test_autocorr_amber_on_strong_periodicity();
	test_autocorr_green_on_low_correlation();
	test_spectral_sweep_stationary_smooth();
	test_spectral_sweep_sinusoid_at_period_4();
	test_spectral_sweep_insufficient_data();
	test_half_split_green_on_stationary();
	test_half_split_amber_on_drift();
	test_half_split_yellow_when_too_few();
	test_overall_is_worst_of_all();
	test_red_overall_propagates_from_constant_stream();
	test_e2e_stationary_should_be_green();

	if (failures > 0) {
		fprintf(stderr, "\n%d assertion(s) failed.\n", failures);
		return 1;
	}
	printf("All D007 verdict tests passed.\n");
	return 0;
}
