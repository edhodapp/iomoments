/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Tests for src/iomoments_level2.h — D013 Level 2 Nyquist confidence
 * and autocorrelation diagnostics.
 *
 * Strategy: synthesize windowed iomoments_summary snapshots from
 * known distributions (stationary Gaussian, drifting mean, aliased
 * periodic content) using a deterministic LCG-driven Box-Muller
 * Gaussian generator, then assert the Level 2 statistics fall in
 * the predicted bands.
 *
 * The thresholds are deliberately generous — Level 2 statistics
 * have CLT noise of their own, and confidence-vs-CLT is a soft
 * scoring function, not a binary classifier. The point of the
 * tests is to pin the *direction* of the response: stationary →
 * high confidence; drift / aliasing → low confidence; and the
 * autocorrelation responses follow the structural prediction.
 *
 * Exit code: 0 = all passed; 1 = at least one failed.
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../src/iomoments_level2.h"
#include "../../src/pebay.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

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
 * Box-Muller Gaussian from a 64-bit LCG. Produces two independent
 * N(0,1) samples per call to the underlying LCG; we cache one and
 * return the other on alternating calls. Deterministic given the
 * starting state.
 */
struct gauss_state {
	uint64_t lcg;
	int has_cached;
	double cached;
};

static double gauss_uniform(struct gauss_state *g)
{
	g->lcg = g->lcg * 6364136223846793005ULL + 1442695040888963407ULL;
	/* Top 53 bits → uniform in [0, 1). Avoid exact 0 for log(). */
	uint64_t u53 = g->lcg >> 11;
	double u = (double)u53 / (double)(1ULL << 53);
	if (u <= 0.0) {
		u = 1e-12;
	}
	return u;
}

static double gauss_next(struct gauss_state *g)
{
	if (g->has_cached) {
		g->has_cached = 0;
		return g->cached;
	}
	double u1 = gauss_uniform(g);
	double u2 = gauss_uniform(g);
	double r = sqrt(-2.0 * log(u1));
	double t = 2.0 * M_PI * u2;
	g->cached = r * sin(t);
	g->has_cached = 1;
	return r * cos(t);
}

/* Build N windows of K i.i.d. samples ~ N(μ, σ²) per window. */
static void gen_stationary(struct iomoments_window *ring, size_t n_windows,
			   size_t k_per_window, double mu, double sigma,
			   uint64_t seed)
{
	struct gauss_state g = {seed, 0, 0.0};
	for (size_t i = 0; i < n_windows; i++) {
		struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
		for (size_t j = 0; j < k_per_window; j++) {
			double sample = mu + sigma * gauss_next(&g);
			iomoments_summary_update(&s, sample);
		}
		ring[i].end_ts_ns = i * 100000000ULL;
		ring[i].summary = s;
	}
}

/*
 * Build N windows where the per-window mean drifts linearly.
 * Within each window samples are still i.i.d. ~ N(μ_i, σ²); μ_i =
 * μ_0 + drift_per_window·i. This is non-stationarity at the slow
 * timescale — windowed-mean variance is inflated by the drift,
 * pushing the Level 2 variance ratio above 1 and dropping the
 * confidence score.
 */
static void gen_drift(struct iomoments_window *ring, size_t n_windows,
		      size_t k_per_window, double mu0, double sigma,
		      double drift_per_window, uint64_t seed)
{
	struct gauss_state g = {seed, 0, 0.0};
	for (size_t i = 0; i < n_windows; i++) {
		double window_mu = mu0 + drift_per_window * (double)i;
		struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
		for (size_t j = 0; j < k_per_window; j++) {
			double sample = window_mu + sigma * gauss_next(&g);
			iomoments_summary_update(&s, sample);
		}
		ring[i].end_ts_ns = i * 100000000ULL;
		ring[i].summary = s;
	}
}

/*
 * Build N windows where the underlying signal is a sinusoid with
 * period exactly equal to one window. Within each window samples
 * span a full sine cycle — so the windowed mean is the cycle
 * average (≈ μ, independent of phase), and the variance of the
 * windowed mean DROPS far below the CLT prediction. This is the
 * classical aliasing dip the paper describes: window length
 * matches hidden period → windowed means become insensitive to
 * phase → V_obs ≪ V_pred → variance ratio ≪ 1 → confidence drops.
 */
static void gen_aliased_periodic(struct iomoments_window *ring,
				 size_t n_windows, size_t k_per_window,
				 double mu, double amplitude, double sigma,
				 uint64_t seed)
{
	struct gauss_state g = {seed, 0, 0.0};
	for (size_t i = 0; i < n_windows; i++) {
		struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
		for (size_t j = 0; j < k_per_window; j++) {
			double phase =
				2.0 * M_PI * (double)j / (double)k_per_window;
			double signal = mu + amplitude * sin(phase);
			double sample = signal + sigma * gauss_next(&g);
			iomoments_summary_update(&s, sample);
		}
		ring[i].end_ts_ns = i * 100000000ULL;
		ring[i].summary = s;
	}
}

/*
 * Helper: aggregate ring → global summary via pebay.h's parallel
 * combine. Mirrors what iomoments.c does at end of duration.
 */
static void aggregate_ring(const struct iomoments_window *ring, size_t count,
			   struct iomoments_summary *global)
{
	*global = (struct iomoments_summary)IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < count; i++) {
		iomoments_summary_merge(global, &ring[i].summary);
	}
}

/* --- Confidence-function unit tests ------------------------------------ */

static void test_confidence_function_calibration(void)
{
	/* r = 1.0 → confidence = 1.0 (perfect agreement). */
	double c1 = iomoments_level2_confidence(1.0);
	CHECK(c1 > 0.999 && c1 <= 1.0);

	/* Symmetric in log space: r and 1/r give the same confidence. */
	double c_2 = iomoments_level2_confidence(2.0);
	double c_half = iomoments_level2_confidence(0.5);
	CHECK(fabs(c_2 - c_half) < 1e-12);

	/* Calibration anchors per the docstring. */
	CHECK(iomoments_level2_confidence(2.0) > 0.55 &&
	      iomoments_level2_confidence(2.0) < 0.65);
	CHECK(iomoments_level2_confidence(4.0) > 0.10 &&
	      iomoments_level2_confidence(4.0) < 0.20);
	CHECK(iomoments_level2_confidence(8.0) > 0.005 &&
	      iomoments_level2_confidence(8.0) < 0.020);

	/* Pathological inputs return 0 (safe). */
	CHECK(iomoments_level2_confidence(0.0) == 0.0);
	CHECK(iomoments_level2_confidence(-1.0) == 0.0);
}

/* --- Behavioral tests on synthetic windowed data ----------------------- */

/*
 * Stationary Gaussian → high Nyquist confidence, low autocorr.
 * For 200 windows × 200 samples each from N(1000, 50²), the CLT
 * prediction is Var(m1_i) ≈ σ²/200 = 12.5. Sample noise on the
 * variance estimator itself is ~√(2/N) relative; with N=200 windows
 * that's ~10%. Confidence > 0.7 is a comfortable threshold above
 * any reasonable run.
 */
static void test_stationary_gaussian_high_confidence(void)
{
	const size_t N = 200;
	const size_t K = 200;
	struct iomoments_window *ring = calloc(N, sizeof(*ring));
	if (!ring) {
		fprintf(stderr, "calloc ring\n");
		failures += 1;
		return;
	}
	gen_stationary(ring, N, K, 1000.0, 50.0, 0xCAFEBABEDEADBEEFULL);

	struct iomoments_summary global;
	aggregate_ring(ring, N, &global);

	struct iomoments_level2_result l2;
	iomoments_level2_analyze(ring, N, &global, &l2);

	CHECK(!l2.insufficient_data);
	CHECK(l2.n_windows == N);
	CHECK(l2.nyquist_confidence > 0.7);
	/* Variance ratio should be near 1, within a factor of 2. */
	CHECK(l2.variance_ratio > 0.5 && l2.variance_ratio < 2.0);
	/* Autocorrelations should be small (within sampling noise of 0). */
	for (size_t li = 0; li < IOMOMENTS_LEVEL2_LAGS; li++) {
		CHECK(fabs(l2.autocorr[li]) < 0.3);
	}

	free(ring);
}

/*
 * Linear drift in the per-window mean → variance of windowed-mean
 * is inflated far above the CLT prediction → confidence drops.
 * Drift = 5 ns/window over 200 windows = 1000 ns total drift,
 * which is 20× σ=50ns, so the windowed-mean variance is
 * dominated by the drift component and r ≫ 1.
 */
static void test_drifting_mean_low_confidence(void)
{
	const size_t N = 200;
	const size_t K = 200;
	struct iomoments_window *ring = calloc(N, sizeof(*ring));
	if (!ring) {
		fprintf(stderr, "calloc ring\n");
		failures += 1;
		return;
	}
	gen_drift(ring, N, K, 1000.0, 50.0, 5.0, 0xFEEDFACECAFEF00DULL);

	struct iomoments_summary global;
	aggregate_ring(ring, N, &global);

	struct iomoments_level2_result l2;
	iomoments_level2_analyze(ring, N, &global, &l2);

	CHECK(!l2.insufficient_data);
	/* Drift inflates variance ratio well above 1. */
	CHECK(l2.variance_ratio > 5.0);
	/* Confidence should drop substantially below stationary case. */
	CHECK(l2.nyquist_confidence < 0.3);
	/* Linear drift produces strong positive autocorrelation at all
	 * tested lags (windowed means trend together). */
	CHECK(l2.autocorr[0] > 0.5);
	CHECK(l2.autocorr[3] > 0.3);

	free(ring);
}

/*
 * Aliased periodic content (period = 1 window) → windowed means
 * are insensitive to phase, V_obs ≪ V_pred. Variance ratio drops
 * far below 1 → confidence drops via the symmetric Gaussian-of-
 * log-ratio. This is the canonical aliasing fingerprint the paper
 * describes.
 *
 * Within-window samples follow a sinusoid with amplitude 200 ns
 * (4× σ=50ns intra-window noise), so the per-sample within-window
 * variance is ~ amplitude²/2 + σ² ≈ 22500 ns². Each windowed
 * mean averages out the sinusoid and lands at μ ± noise/√K ≈
 * μ ± 4ns. CLT predicts σ_global²/K ≈ 22500/200 ≈ 112 ns². V_obs
 * is ~16ns² (purely from the residual within-window noise on the
 * mean). r ≈ 0.14 → confidence < 0.1.
 */
static void test_aliased_periodic_low_confidence(void)
{
	const size_t N = 200;
	const size_t K = 200;
	struct iomoments_window *ring = calloc(N, sizeof(*ring));
	if (!ring) {
		fprintf(stderr, "calloc ring\n");
		failures += 1;
		return;
	}
	gen_aliased_periodic(ring, N, K, 1000.0, 200.0, 50.0,
			     0xABCDEF1234567890ULL);

	struct iomoments_summary global;
	aggregate_ring(ring, N, &global);

	struct iomoments_level2_result l2;
	iomoments_level2_analyze(ring, N, &global, &l2);

	CHECK(!l2.insufficient_data);
	/* Variance-of-windowed-mean dips far below CLT prediction. */
	CHECK(l2.variance_ratio < 0.5);
	/* Confidence drops accordingly — should be well under stationary. */
	CHECK(l2.nyquist_confidence < 0.5);

	free(ring);
}

/*
 * Insufficient-data guard: with < 4 windows, analyze() returns the
 * insufficient_data flag and zero stats, so downstream callers
 * skip Level 2 emission cleanly.
 */
static void test_insufficient_data_flag(void)
{
	const size_t N = 3;
	struct iomoments_window ring[3];
	gen_stationary(ring, N, 100, 1000.0, 50.0, 0x12345ULL);

	struct iomoments_summary global;
	aggregate_ring(ring, N, &global);

	struct iomoments_level2_result l2;
	iomoments_level2_analyze(ring, N, &global, &l2);

	CHECK(l2.insufficient_data == 1);
	CHECK(l2.nyquist_confidence == 0.0);
}

int main(void)
{
	test_confidence_function_calibration();
	test_stationary_gaussian_high_confidence();
	test_drifting_mean_low_confidence();
	test_aliased_periodic_low_confidence();
	test_insufficient_data_flag();

	if (failures > 0) {
		fprintf(stderr, "\n%d assertion(s) failed.\n", failures);
		return 1;
	}
	printf("All Level 2 (D013) Nyquist-confidence tests passed.\n");
	return 0;
}
