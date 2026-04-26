/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Monte Carlo statistical tests for iomoments.
 *
 * The deterministic tests in tests/c/test_*.c pin specific
 * fixture-seed-pair behaviors — good for catching regressions but
 * silent about the *probability* that the band assertions hold
 * under random seeds. Several claims in the codebase are
 * specifically statistical:
 *
 *   - "Hill standard error α/√k ≈ 17% at α=1, 30% at α=3"
 *     (iomoments_topk.h docstring)
 *   - "Level 2 Nyquist confidence > 0.7 for stationary Gaussian"
 *     (test_level2.c assertion)
 *   - "Spectral sweep detects aliasing at the W' multiple of the
 *     true period" (test_verdict.c assertion)
 *
 * Monte Carlo runs N trials with randomized seeds, computes the
 * empirical pass rate, and asserts pass-rate ≥ threshold. False
 * negatives (test fails when implementation is correct) are
 * possible — they mean the band tightness chosen at fixture
 * time was over-optimistic.
 *
 * NOT in the normal gate (`make test-c`). Run periodically via
 * `make test-mc` (e.g. weekly cron or a separate CI workflow).
 *
 * Trial count: default 100, override via IOMOMENTS_MC_TRIALS env
 * var. Seed: time(NULL) at startup, advanced per trial. To
 * reproduce a failing run, dump the start seed at run start
 * (printed always) and re-run with IOMOMENTS_MC_SEED set to it.
 *
 * Exit code: 0 = all MC fixtures cleared their pass-rate
 * thresholds; 1 = at least one didn't.
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../../src/iomoments_level2.h"
#include "../../src/iomoments_spectral.h"
#include "../../src/iomoments_topk.h"
#include "../../src/iomoments_verdict.h"
#include "../../src/pebay.h"

static int failures;
static size_t mc_trials;
static uint64_t mc_root_seed;

#define CHECK_RATE(pass_count, total, threshold, fixture_name)                 \
	do {                                                                   \
		double rate = (double)(pass_count) / (double)(total);          \
		fprintf(stdout,                                                \
			"  %-44s %5zu/%-5zu (%.1f%%)  threshold %.1f%%  %s\n", \
			(fixture_name), (size_t)(pass_count), (size_t)(total), \
			rate * 100, (threshold) * 100,                         \
			rate >= (threshold) ? "ok" : "FAIL");                  \
		if (rate < (threshold)) {                                      \
			failures += 1;                                         \
		}                                                              \
	} while (0)

/* Deterministic LCG, seeded per trial. */
static uint64_t lcg_next(uint64_t *state)
{
	*state = (*state) * 6364136223846793005ULL + 1442695040888963407ULL;
	return *state;
}

static double lcg_uniform(uint64_t *state)
{
	uint64_t u53 = lcg_next(state) >> 11;
	double u = (double)u53 / (double)(1ULL << 53);
	if (u <= 0.0)
		u = 1e-12;
	return u;
}

static double gauss_next(uint64_t *state, int *has_cached, double *cached)
{
	if (*has_cached) {
		*has_cached = 0;
		return *cached;
	}
	double u1 = lcg_uniform(state);
	double u2 = lcg_uniform(state);
	double r = sqrt(-2.0 * log(u1));
	double t = 2.0 * 3.14159265358979323846 * u2;
	*cached = r * sin(t);
	*has_cached = 1;
	return r * cos(t);
}

static double pareto_sample(double alpha, uint64_t *state)
{
	double u = lcg_uniform(state);
	return pow(u, -1.0 / alpha);
}

/* --- Hill estimator: empirical mean + std on Pareto(α) ---------------- */

/*
 * For each trial, draw 10000 Pareto(α) samples with a fresh seed,
 * compute Hill α̂ over the K=32 reservoir. Across trials, count
 * how many α̂ fall within ±2·(α/√k) of the true α. Theory says
 * ~95% (under normality of α̂; Hill is asymptotically normal for
 * Pareto). Threshold at 85% to absorb finite-sample skew + the
 * non-asymptotic regime at small k.
 */
static void mc_hill_pareto(double alpha, double pass_threshold,
			   const char *name)
{
	size_t pass = 0;
	double k_inv_sqrt = 1.0 / sqrt((double)IOMOMENTS_TOPK_K);
	double tolerance = 2.0 * alpha * k_inv_sqrt;
	for (size_t t = 0; t < mc_trials; t++) {
		uint64_t state = mc_root_seed + (t + 1) * 0x9E3779B97F4A7C15ULL;
		struct iomoments_topk r;
		iomoments_topk_init(&r);
		for (size_t i = 0; i < 10000; i++) {
			double s = pareto_sample(alpha, &state);
			uint64_t q = (uint64_t)(s * 1e6);
			if (q == 0)
				q = 1;
			iomoments_topk_insert(&r, q);
		}
		double alpha_hat = iomoments_hill_estimator(&r);
		if (alpha_hat == 0.0)
			continue;
		if (fabs(alpha_hat - alpha) <= tolerance) {
			pass += 1;
		}
	}
	CHECK_RATE(pass, mc_trials, pass_threshold, name);
}

/* --- Level 2 Nyquist confidence on stationary Gaussian ---------------- */

static void mc_level2_stationary_high_confidence(void)
{
	size_t pass = 0;
	const size_t N = 200;
	const size_t K = 200;
	struct iomoments_window *ring = calloc(N, sizeof(*ring));
	if (!ring) {
		fprintf(stderr, "calloc\n");
		failures += 1;
		return;
	}
	for (size_t t = 0; t < mc_trials; t++) {
		uint64_t state = mc_root_seed + (t + 1) * 0x6DEAD1234ABCD123ULL;
		int has_cached = 0;
		double cached = 0.0;
		for (size_t i = 0; i < N; i++) {
			struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
			for (size_t j = 0; j < K; j++) {
				double samp =
					1000.0 + 50.0 * gauss_next(&state,
								   &has_cached,
								   &cached);
				iomoments_summary_update(&s, samp);
			}
			ring[i].end_ts_ns = i * 100000000ULL;
			ring[i].summary = s;
			iomoments_topk_init(&ring[i].topk);
		}
		struct iomoments_summary global = IOMOMENTS_SUMMARY_ZERO;
		for (size_t i = 0; i < N; i++) {
			iomoments_summary_merge(&global, &ring[i].summary);
		}
		struct iomoments_level2_result l2;
		iomoments_level2_analyze(ring, N, &global, &l2);
		if (l2.nyquist_confidence > 0.5) {
			pass += 1;
		}
	}
	free(ring);
	CHECK_RATE(pass, mc_trials, 0.90, "level2 stationary nyquist > 0.5");
}

/* --- Spectral sweep detects aliasing on periodic data ---------------- */

static void mc_spectral_detects_aliasing(void)
{
	size_t pass = 0;
	const size_t N = 128;
	const size_t K = 200;
	const double period_in_windows = 4.0;
	struct iomoments_window *ring = calloc(N, sizeof(*ring));
	if (!ring) {
		fprintf(stderr, "calloc\n");
		failures += 1;
		return;
	}
	for (size_t t = 0; t < mc_trials; t++) {
		uint64_t state = mc_root_seed + (t + 1) * 0xFEEDFACE89ABCDEFULL;
		int has_cached = 0;
		double cached = 0.0;
		for (size_t i = 0; i < N; i++) {
			double window_mu =
				1000.0 +
				300.0 * sin(2.0 * 3.14159265358979323846 *
					    (double)i / period_in_windows);
			struct iomoments_summary s = IOMOMENTS_SUMMARY_ZERO;
			for (size_t j = 0; j < K; j++) {
				double samp = window_mu +
					      20.0 * gauss_next(&state,
								&has_cached,
								&cached);
				iomoments_summary_update(&s, samp);
			}
			ring[i].end_ts_ns = i * 100000000ULL;
			ring[i].summary = s;
			iomoments_topk_init(&ring[i].topk);
		}
		struct iomoments_summary global = IOMOMENTS_SUMMARY_ZERO;
		for (size_t i = 0; i < N; i++) {
			iomoments_summary_merge(&global, &ring[i].summary);
		}
		struct iomoments_spectral_result spec;
		iomoments_spectral_sweep(ring, N, &global, 0.1, &spec);
		if (!spec.insufficient_data && spec.min_ratio < 0.2) {
			/* Localized W' must be a multiple of the true period. */
			double w_at_min =
				spec.points[spec.min_ratio_idx].window_seconds;
			double frac = w_at_min / 0.4;
			if (fabs(frac - round(frac)) < 1e-9) {
				pass += 1;
			}
		}
	}
	free(ring);
	CHECK_RATE(pass, mc_trials, 0.90,
		   "spectral_sweep detects period-4 aliasing");
}

int main(void)
{
	const char *trials_env = getenv("IOMOMENTS_MC_TRIALS");
	mc_trials = trials_env ? (size_t)atol(trials_env) : 100;
	if (mc_trials == 0)
		mc_trials = 100;
	const char *seed_env = getenv("IOMOMENTS_MC_SEED");
	mc_root_seed = seed_env ? (uint64_t)strtoull(seed_env, NULL, 0)
				: (uint64_t)time(NULL);

	printf("Monte Carlo iomoments tests\n");
	printf("  trials per fixture: %zu\n", mc_trials);
	printf("  root seed         : 0x%016llx\n",
	       (unsigned long long)mc_root_seed);
	printf("  reproduce a fail  : IOMOMENTS_MC_SEED=0x%llx make test-mc\n",
	       (unsigned long long)mc_root_seed);
	printf("\n  fixture                                       pass / total"
	       "    threshold      result\n");
	printf("  ------------------------------------------------------------"
	       "------------------------\n");

	mc_hill_pareto(0.6, 0.85, "hill α=0.6  ±2·SE");
	mc_hill_pareto(1.5, 0.85, "hill α=1.5  ±2·SE");
	mc_hill_pareto(2.0, 0.85, "hill α=2.0  ±2·SE");
	mc_hill_pareto(3.5, 0.85, "hill α=3.5  ±2·SE");
	mc_level2_stationary_high_confidence();
	mc_spectral_detects_aliasing();

	if (failures > 0) {
		fprintf(stderr,
			"\n%d MC fixture(s) failed pass-rate threshold.\n",
			failures);
		return 1;
	}
	printf("\nAll Monte Carlo fixtures cleared their thresholds.\n");
	return 0;
}
