/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * iomoments Level 2 analysis (D013).
 *
 * Level 1 (BPF, periodically drained by iomoments.c) produces a time
 * series of windowed iomoments_summary snapshots. Level 2 takes that
 * time series and computes the statistics that diagnose Nyquist
 * conformance and stationarity:
 *
 *   - variance of the windowed-mean stream
 *   - CLT-predicted variance under stationary, Nyquist-met
 *     assumptions: σ²_global / n_per_window
 *   - the ratio of observed to predicted (the Nyquist-confidence
 *     fingerprint)
 *   - lag-k autocorrelation of windowed means at fixed lags
 *
 * For stationary, Nyquist-met data the CLT predicts:
 *
 *   E[Var(m1_i)] = σ² / n_per_window
 *   E[autocorr(m1_i, m1_{i+k})] = 0  (∀k > 0)
 *
 * The confidence score is built from how close the observed
 * statistics come to those predictions. Aliasing pushes the
 * variance ratio away from 1 (typically downward — windowed means
 * become phase-insensitive when window matches period — but
 * non-stationarity inflates it instead). Persistent autocorrelation
 * at any tested lag is a separate flag.
 *
 * Userspace-only — uses doubles via pebay.h. No BPF compilation.
 *
 * Header-only by convention (mirrors pebay.h). All functions are
 * static inline; symbols are local to each translation unit that
 * includes this header.
 */

#ifndef IOMOMENTS_LEVEL2_H
#define IOMOMENTS_LEVEL2_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "iomoments_topk.h"
#include "pebay.h"

/*
 * One windowed snapshot in the Level 2 input ring. `end_ts_ns` is
 * the CLOCK_MONOTONIC time at which userspace finished draining the
 * BPF per-CPU map; `summary` is the merged iomoments_summary across
 * CPUs for that window; `topk` is the merged top-K reservoir across
 * CPUs for the same window (input to the Hill tail-index signal at
 * verdict-compute time).
 */
struct iomoments_window {
	uint64_t end_ts_ns;
	struct iomoments_summary summary;
	struct iomoments_topk topk;
};

/*
 * Fixed lag set for Level 2 autocorrelation. Powers of 2 cover
 * doubling-period periodic content; 3 picks up small odd primes.
 * Keeping it small + fixed avoids unbounded loops the verifier
 * would care about (Level 2 is userspace, but the fixed shape
 * also keeps the report tidy).
 */
#define IOMOMENTS_LEVEL2_LAGS 4
static const size_t iomoments_level2_lag_values[IOMOMENTS_LEVEL2_LAGS] = {1, 2,
									  4, 8};

struct iomoments_level2_result {
	size_t n_windows; /* windows with non-empty samples */
	double avg_samples_per_window;

	double mean_of_windowed_mean; /* E[m1_i]   */
	double var_of_windowed_mean;  /* Var[m1_i] */

	double clt_predicted_var; /* σ²_global / n_per_window */
	double variance_ratio;	  /* observed / predicted */

	/*
	 * Nyquist confidence in [0, 1]. 1 = high confidence the data
	 * is consistent with stationary Nyquist-met sampling; 0 =
	 * strong evidence against. Built from the variance-ratio
	 * deviation; see compute_confidence below.
	 */
	double nyquist_confidence;

	/* Lag-k autocorrelation of windowed means at the fixed lag set. */
	double autocorr[IOMOMENTS_LEVEL2_LAGS];

	/*
	 * Set to 1 if the analysis was below the minimum-windows
	 * threshold and statistics are not meaningful. Caller should
	 * skip emitting Level 2 in that case.
	 */
	int insufficient_data;
};

/*
 * Convert variance ratio → Nyquist confidence.
 *
 * For r = V_obs / V_pred:
 *   r → 1: confidence → 1
 *   r → 0 or ∞: confidence → 0
 *
 * Use a Gaussian-of-log-ratio:
 *   confidence = exp( -½ · (log₂ r)² )
 *
 * Calibration:
 *   r = 1.0   → 1.000  (perfect agreement)
 *   r = 1.5   → 0.875  (mild deviation, still high confidence)
 *   r = 2.0   → 0.607  (medium — flag yellow)
 *   r = 4.0   → 0.135  (strong — flag amber)
 *   r = 8.0   → 0.011  (very strong — flag amber/red)
 *
 * Symmetric in log space, so dips and inflations are penalized
 * equally. Note that aliasing can push r low *or* high (typically
 * low when window matches period; high when non-stationary).
 */
static inline double iomoments_level2_confidence(double variance_ratio)
{
	if (variance_ratio <= 0.0) {
		return 0.0;
	}
	/* variance_ratio > 0 here by the guard above; cppcheck's data
	 * flow doesn't track the early return. */
	/* cppcheck-suppress invalidFunctionArg */
	double log_r = log2(variance_ratio);
	return exp(-0.5 * log_r * log_r);
}

/*
 * Compute lag-k autocorrelation of the windowed-mean time series.
 * Pearson form: ρ_k = Σ (m1_i − μ)(m1_{i+k} − μ) / (N · σ²_m1).
 *
 * Returns 0 if there are too few windows for the lag, or if the
 * windowed-mean variance is zero (constant stream).
 */
static inline double
iomoments_level2_autocorr(const struct iomoments_window *ring, size_t count,
			  size_t lag, double mean_of_m1, double var_of_m1)
{
	if (count <= lag || var_of_m1 <= 0.0) {
		return 0.0;
	}
	double sum = 0.0;
	size_t pairs = 0;
	for (size_t i = 0; i + lag < count; i++) {
		if (ring[i].summary.n == 0 || ring[i + lag].summary.n == 0) {
			continue;
		}
		double d1 = ring[i].summary.m1 - mean_of_m1;
		double d2 = ring[i + lag].summary.m1 - mean_of_m1;
		sum += d1 * d2;
		pairs += 1;
	}
	if (pairs == 0) {
		return 0.0;
	}
	return (sum / (double)pairs) / var_of_m1;
}

/*
 * Run the full Level 2 analysis on a window-snapshot ring. Caller
 * provides the global aggregate (already computed via pebay.h's
 * parallel-combine over the ring) so we don't redundantly merge.
 *
 * Minimum 4 non-empty windows for results to be meaningful; below
 * that, marks `insufficient_data` and returns. The threshold is
 * generous — autocorrelation at lag-8 needs at least 9 windows for
 * any pair, lag-1 needs 2 — but Level 2 statistics are noisy below
 * ~10 windows and we don't want to mislead downstream verdicts.
 */
static inline void
iomoments_level2_analyze(const struct iomoments_window *ring, size_t count,
			 const struct iomoments_summary *global,
			 struct iomoments_level2_result *result)
{
	memset(result, 0, sizeof(*result));

	/* Build the distribution of m1_i across non-empty windows. */
	struct iomoments_summary m1_dist = IOMOMENTS_SUMMARY_ZERO;
	uint64_t total_n = 0;
	size_t valid = 0;
	for (size_t i = 0; i < count; i++) {
		if (ring[i].summary.n > 0) {
			iomoments_summary_update(&m1_dist, ring[i].summary.m1);
			total_n += ring[i].summary.n;
			valid += 1;
		}
	}

	result->n_windows = valid;
	if (valid < 4) {
		result->insufficient_data = 1;
		return;
	}
	result->avg_samples_per_window = (double)total_n / (double)valid;
	result->mean_of_windowed_mean = iomoments_summary_mean(&m1_dist);
	result->var_of_windowed_mean = iomoments_summary_variance(&m1_dist);

	double global_var = iomoments_summary_variance(global);
	if (result->avg_samples_per_window > 0.0 && global_var > 0.0) {
		result->clt_predicted_var =
			global_var / result->avg_samples_per_window;
		if (result->clt_predicted_var > 0.0) {
			result->variance_ratio = result->var_of_windowed_mean /
						 result->clt_predicted_var;
		}
		result->nyquist_confidence =
			iomoments_level2_confidence(result->variance_ratio);
	}

	for (size_t li = 0; li < IOMOMENTS_LEVEL2_LAGS; li++) {
		result->autocorr[li] = iomoments_level2_autocorr(
			ring, count, iomoments_level2_lag_values[li],
			result->mean_of_windowed_mean,
			result->var_of_windowed_mean);
	}
}

#endif /* IOMOMENTS_LEVEL2_H */
