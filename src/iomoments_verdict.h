/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * iomoments diagnostic-verdict layer (D007).
 *
 * Consumes the Level 1 moments (mean/variance/skewness/excess
 * kurtosis from pebay.h) and Level 2 statistics (Nyquist confidence,
 * lagged autocorrelation, half-split stability from
 * iomoments_level2.h plus the window ring) and emits a per-run
 * verdict in {GREEN, YELLOW, AMBER, RED}.
 *
 * Per D007:
 *   GREEN  — moments are a trustworthy shape summary for this
 *            workload. Emit moments with expected error budget.
 *   YELLOW — moments are informative but miss some structure
 *            (e.g., bimodality, mild non-stationarity). Emit
 *            moments with caveats.
 *   AMBER  — moments are likely biased (aliasing suspected,
 *            heavy-tail signature, large half-split drift). Emit
 *            moments with a diagnostic recommendation.
 *   RED    — moments are the wrong primitive for this workload
 *            (constant stream, too few samples, or pathological
 *            distribution). Refuse to emit a moment-based
 *            summary; recommend an alternative tool (DDSketch,
 *            HDR Histogram).
 *
 * Each evaluator emits a signal with its own status and a short
 * rationale string. Overall verdict is the worst-of-all (max of
 * the signal statuses by enum order). The set of evaluators
 * implemented today:
 *
 *   1. sample_count        — n thresholds (red < 100, yellow < 1000)
 *   2. variance_sanity     — σ² == 0 → red (constant stream)
 *   3. nyquist_confidence  — Level 2 confidence-vs-CLT bands
 *   4. autocorr_residual   — max(|autocorr[k]|) thresholds
 *   5. half_split_stability — split ring at midpoint, compare
 *                              mean drift in σ-units + variance
 *                              ratio between halves
 *   6. kurtosis_sanity     — extreme excess kurtosis flag
 *   7. carleman_partial_sum — moment-determinacy proxy from the
 *                              first two terms of the Carleman
 *                              series (μ₂/n)^(-½) + (μ₄/n)^(-¼);
 *                              the ratio of term-2 to term-1 is
 *                              a tail-weight diagnostic
 *
 * Future evaluators (separate commits) named in D007 / D013:
 *   - Hankel matrix conditioning
 *   - Hill tail-index (needs order-statistic reservoir)
 *   - KS goodness-of-fit to log-normal (needs empirical CDF)
 *   - Spectral flatness sweep (varies window length)
 *
 * Userspace-only — uses doubles. Header-only by convention.
 */

#ifndef IOMOMENTS_VERDICT_H
#define IOMOMENTS_VERDICT_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "iomoments_level2.h"
#include "pebay.h"

enum iomoments_verdict_status {
	IOMOMENTS_VERDICT_GREEN = 0,
	IOMOMENTS_VERDICT_YELLOW = 1,
	IOMOMENTS_VERDICT_AMBER = 2,
	IOMOMENTS_VERDICT_RED = 3,
};

#define IOMOMENTS_VERDICT_RATIONALE_MAX 192
#define IOMOMENTS_VERDICT_MAX_SIGNALS 8

struct iomoments_verdict_signal {
	const char *name;
	enum iomoments_verdict_status status;
	char rationale[IOMOMENTS_VERDICT_RATIONALE_MAX];
};

struct iomoments_verdict {
	enum iomoments_verdict_status overall;
	size_t n_signals;
	struct iomoments_verdict_signal signals[IOMOMENTS_VERDICT_MAX_SIGNALS];
};

static inline const char *
iomoments_verdict_status_name(enum iomoments_verdict_status s)
{
	switch (s) {
	case IOMOMENTS_VERDICT_GREEN:
		return "GREEN";
	case IOMOMENTS_VERDICT_YELLOW:
		return "YELLOW";
	case IOMOMENTS_VERDICT_AMBER:
		return "AMBER";
	case IOMOMENTS_VERDICT_RED:
		return "RED";
	}
	return "UNKNOWN";
}

/*
 * Push a signal onto the verdict, advancing the overall to the
 * worst-of-all. Internal helper for the evaluators below.
 */
static inline void iomoments_verdict_push(struct iomoments_verdict *v,
					  const char *name,
					  enum iomoments_verdict_status status,
					  const char *rationale)
{
	if (v->n_signals >= IOMOMENTS_VERDICT_MAX_SIGNALS) {
		return;
	}
	struct iomoments_verdict_signal *s = &v->signals[v->n_signals];
	s->name = name;
	s->status = status;
	if (rationale != NULL) {
		size_t len = strlen(rationale);
		if (len >= IOMOMENTS_VERDICT_RATIONALE_MAX) {
			len = IOMOMENTS_VERDICT_RATIONALE_MAX - 1;
		}
		memcpy(s->rationale, rationale, len);
		s->rationale[len] = '\0';
	} else {
		s->rationale[0] = '\0';
	}
	v->n_signals += 1;
	if ((int)status > (int)v->overall) {
		v->overall = status;
	}
}

/* --- Individual signal evaluators ------------------------------------- */

static inline void
iomoments_verdict_eval_sample_count(const struct iomoments_summary *global,
				    struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (global->n < 100) {
		snprintf(r, sizeof(r),
			 "n=%lu < 100; insufficient for moment estimation",
			 (unsigned long)global->n);
		iomoments_verdict_push(v, "sample_count", IOMOMENTS_VERDICT_RED,
				       r);
		return;
	}
	if (global->n < 1000) {
		snprintf(r, sizeof(r), "n=%lu < 1000; estimator noise is high",
			 (unsigned long)global->n);
		iomoments_verdict_push(v, "sample_count",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	snprintf(r, sizeof(r), "n=%lu", (unsigned long)global->n);
	iomoments_verdict_push(v, "sample_count", IOMOMENTS_VERDICT_GREEN, r);
}

static inline void
iomoments_verdict_eval_variance_sanity(const struct iomoments_summary *global,
				       struct iomoments_verdict *v)
{
	double var = iomoments_summary_variance(global);
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (var <= 0.0) {
		snprintf(r, sizeof(r),
			 "σ² = 0; constant stream, moments undefined");
		iomoments_verdict_push(v, "variance_sanity",
				       IOMOMENTS_VERDICT_RED, r);
		return;
	}
	snprintf(r, sizeof(r), "σ² = %.3f ns²", var);
	iomoments_verdict_push(v, "variance_sanity", IOMOMENTS_VERDICT_GREEN,
			       r);
}

static inline void
iomoments_verdict_eval_kurtosis_sanity(const struct iomoments_summary *global,
				       struct iomoments_verdict *v)
{
	double k = iomoments_summary_excess_kurtosis(global);
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	/* Excess kurtosis > 12 indicates very heavy tails (Gaussian = 0,
	 * exponential = 6, log-normal-ish = up to ~10). Above 12 the
	 * moment-based summary is increasingly unrepresentative of the
	 * tail; flag amber. > 50 is essentially a degenerate spike. */
	if (k > 50.0) {
		snprintf(r, sizeof(r),
			 "excess kurt = %.1f; degenerate / heavy spike,"
			 " moments unreliable",
			 k);
		iomoments_verdict_push(v, "kurtosis_sanity",
				       IOMOMENTS_VERDICT_RED, r);
		return;
	}
	if (k > 12.0) {
		snprintf(r, sizeof(r),
			 "excess kurt = %.1f; heavy-tailed,"
			 " moments may be biased",
			 k);
		iomoments_verdict_push(v, "kurtosis_sanity",
				       IOMOMENTS_VERDICT_AMBER, r);
		return;
	}
	if (k > 6.0) {
		snprintf(r, sizeof(r),
			 "excess kurt = %.1f; moderately heavy-tailed", k);
		iomoments_verdict_push(v, "kurtosis_sanity",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	snprintf(r, sizeof(r), "excess kurt = %+.2f", k);
	iomoments_verdict_push(v, "kurtosis_sanity", IOMOMENTS_VERDICT_GREEN,
			       r);
}

static inline void
iomoments_verdict_eval_nyquist(const struct iomoments_level2_result *l2,
			       struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (l2->insufficient_data) {
		snprintf(r, sizeof(r),
			 "n_windows=%zu < 4; Level 2 not evaluated",
			 l2->n_windows);
		iomoments_verdict_push(v, "nyquist_confidence",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double c = l2->nyquist_confidence;
	double ratio = l2->variance_ratio;
	if (c < 0.1) {
		snprintf(r, sizeof(r),
			 "confidence=%.2f, V/V₀=%.2f; aliasing or strong"
			 " non-stationarity suspected",
			 c, ratio);
		iomoments_verdict_push(v, "nyquist_confidence",
				       IOMOMENTS_VERDICT_AMBER, r);
		return;
	}
	if (c < 0.5) {
		snprintf(r, sizeof(r),
			 "confidence=%.2f, V/V₀=%.2f; moments may miss"
			 " structure",
			 c, ratio);
		iomoments_verdict_push(v, "nyquist_confidence",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	snprintf(r, sizeof(r), "confidence=%.2f, V/V₀=%.2f", c, ratio);
	iomoments_verdict_push(v, "nyquist_confidence", IOMOMENTS_VERDICT_GREEN,
			       r);
}

static inline void
iomoments_verdict_eval_autocorr(const struct iomoments_level2_result *l2,
				struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (l2->insufficient_data) {
		return; /* covered by nyquist_confidence signal already */
	}
	double max_abs = 0.0;
	size_t max_lag = 0;
	for (size_t i = 0; i < IOMOMENTS_LEVEL2_LAGS; i++) {
		double a = fabs(l2->autocorr[i]);
		if (a > max_abs) {
			max_abs = a;
			max_lag = iomoments_level2_lag_values[i];
		}
	}
	if (max_abs > 0.5) {
		snprintf(r, sizeof(r),
			 "max |autocorr| = %.2f at lag %zu;"
			 " periodicity at lag·W detected",
			 max_abs, max_lag);
		iomoments_verdict_push(v, "autocorr_residual",
				       IOMOMENTS_VERDICT_AMBER, r);
		return;
	}
	if (max_abs > 0.3) {
		snprintf(r, sizeof(r),
			 "max |autocorr| = %.2f at lag %zu; mild"
			 " periodic structure",
			 max_abs, max_lag);
		iomoments_verdict_push(v, "autocorr_residual",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	snprintf(r, sizeof(r), "max |autocorr| = %.2f at lag %zu", max_abs,
		 max_lag);
	iomoments_verdict_push(v, "autocorr_residual", IOMOMENTS_VERDICT_GREEN,
			       r);
}

/*
 * Carleman partial-sum diagnostic.
 *
 * Carleman's criterion (Carleman 1926) states that a positive
 * measure on [0, ∞) is moment-determinate if
 *   Σ_{k=1..∞}  (E[X^{2k}])^{−1/(2k)}  =  ∞
 *
 * Equivalently for central moments μ_{2k} = E[(X−μ)^{2k}], the
 * sum Σ μ_{2k}^{−1/(2k)} diverges for moment-determinate
 * distributions. Slow growth or convergence ⇒ heavy-tailed,
 * possibly indeterminate (canonical counterexample: log-normal).
 *
 * iomoments tracks central moments up to k=4. The Carleman
 * partial sum we can compute is just the first two terms:
 *
 *   S_2  =  (μ_2)^{−1/2}  +  (μ_4)^{−1/4}
 *
 * Two terms is too few to definitively conclude divergence vs
 * convergence. But the *ratio* of the second term to the first
 * is informative: how fast are the terms decaying?
 *
 *   ratio  =  (μ_4)^{−1/4} / (μ_2)^{−1/2}
 *
 * Reference values (computed analytically):
 *   Gaussian:    ratio = 1 / 3^{1/4}  ≈ 0.760
 *   exponential: ratio ≈ 0.640
 *   uniform:     ratio ≈ 0.741
 *   log-normal (heavy tail): ratio → 0 as σ_log grows
 *
 * Bands:
 *   ratio > 0.5  → GREEN  (light tail, terms decay slowly,
 *                          consistent with divergent Carleman sum)
 *   0.3 .. 0.5   → YELLOW (moderate tail; partial-sum decay
 *                          faster than Gaussian, caveat the user)
 *   ≤ 0.3        → AMBER  (heavy tail; second-term collapse
 *                          suggests possible moment-indeterminacy)
 *
 * Doesn't go RED on its own — two terms can't definitively
 * establish indeterminacy. The kurtosis_sanity signal already
 * covers degenerate-spike RED separately.
 */
static inline void
iomoments_verdict_eval_carleman(const struct iomoments_summary *global,
				struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (global->n == 0 || global->m2 <= 0.0 || global->m4 <= 0.0) {
		snprintf(r, sizeof(r),
			 "n=%lu, m2=%.3g, m4=%.3g; cannot evaluate",
			 (unsigned long)global->n, global->m2, global->m4);
		iomoments_verdict_push(v, "carleman_partial_sum",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double n = (double)global->n;
	double mu2 = global->m2 / n;
	double mu4 = global->m4 / n;
	if (mu2 <= 0.0 || mu4 <= 0.0) {
		snprintf(r, sizeof(r),
			 "central μ_2 or μ_4 ≤ 0; cannot evaluate");
		iomoments_verdict_push(v, "carleman_partial_sum",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double term1 = pow(mu2, -0.5);
	double term2 = pow(mu4, -0.25);
	double ratio = term2 / term1;
	enum iomoments_verdict_status status;
	if (ratio > 0.5) {
		status = IOMOMENTS_VERDICT_GREEN;
	} else if (ratio > 0.3) {
		status = IOMOMENTS_VERDICT_YELLOW;
	} else {
		status = IOMOMENTS_VERDICT_AMBER;
	}
	snprintf(r, sizeof(r),
		 "term2/term1 = %.3f (Gaussian≈0.76, exp≈0.64;"
		 " lower → heavier tail)",
		 ratio);
	iomoments_verdict_push(v, "carleman_partial_sum", status, r);
}

static inline void
iomoments_verdict_eval_half_split(const struct iomoments_window *ring,
				  size_t count, struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (count < 8) {
		snprintf(r, sizeof(r),
			 "n_windows=%zu < 8; half-split inconclusive", count);
		iomoments_verdict_push(v, "half_split_stability",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	size_t mid = count / 2;
	struct iomoments_summary first = IOMOMENTS_SUMMARY_ZERO;
	struct iomoments_summary second = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < mid; i++) {
		iomoments_summary_merge(&first, &ring[i].summary);
	}
	for (size_t i = mid; i < count; i++) {
		iomoments_summary_merge(&second, &ring[i].summary);
	}
	if (first.n == 0 || second.n == 0) {
		snprintf(r, sizeof(r), "one half empty; cannot compare");
		iomoments_verdict_push(v, "half_split_stability",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double m1_a = iomoments_summary_mean(&first);
	double m1_b = iomoments_summary_mean(&second);
	double v_a = iomoments_summary_variance(&first);
	double v_b = iomoments_summary_variance(&second);
	double pooled_var = (v_a + v_b) * 0.5;
	double pooled_sd = pooled_var > 0.0 ? sqrt(pooled_var) : 0.0;
	double mean_shift_in_sd =
		pooled_sd > 0.0 ? fabs(m1_a - m1_b) / pooled_sd : 0.0;
	double var_ratio = 1.0;
	if (v_a > 0.0 && v_b > 0.0) {
		var_ratio = v_a > v_b ? v_a / v_b : v_b / v_a;
	}
	enum iomoments_verdict_status status = IOMOMENTS_VERDICT_GREEN;
	if (mean_shift_in_sd > 1.0 || var_ratio > 4.0) {
		status = IOMOMENTS_VERDICT_AMBER;
	} else if (mean_shift_in_sd > 0.5 || var_ratio > 2.0) {
		status = IOMOMENTS_VERDICT_YELLOW;
	}
	snprintf(r, sizeof(r), "mean shift %.2f σ_pooled, σ²-ratio %.2f",
		 mean_shift_in_sd, var_ratio);
	iomoments_verdict_push(v, "half_split_stability", status, r);
}

/* --- Top-level compute ------------------------------------------------- */

static inline void
iomoments_verdict_compute(const struct iomoments_summary *global,
			  const struct iomoments_window *ring, size_t count,
			  const struct iomoments_level2_result *l2,
			  struct iomoments_verdict *out)
{
	memset(out, 0, sizeof(*out));
	out->overall = IOMOMENTS_VERDICT_GREEN;

	iomoments_verdict_eval_sample_count(global, out);
	iomoments_verdict_eval_variance_sanity(global, out);
	iomoments_verdict_eval_kurtosis_sanity(global, out);
	iomoments_verdict_eval_carleman(global, out);
	iomoments_verdict_eval_nyquist(l2, out);
	iomoments_verdict_eval_autocorr(l2, out);
	iomoments_verdict_eval_half_split(ring, count, out);
}

#endif /* IOMOMENTS_VERDICT_H */
