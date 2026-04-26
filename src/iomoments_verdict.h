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
 *   8. hankel_conditioning  — 3×3 central-moment Hankel matrix
 *                              determinant, normalized by μ₂³;
 *                              detects low-rank atomic moment
 *                              sequences (degenerate measures
 *                              concentrated on few points) per
 *                              Curto-Fialkow 1991
 *   9. spectral_sweep        — sweeps virtual window length W' =
 *                              k·W across k in {1, 2, 4, ...},
 *                              minimum var_obs/var_pred ratio
 *                              localizes hidden periodicity at
 *                              the W' that triggered it
 *  10. hill_tail_index        — Hill (1975) tail-index estimator
 *                              over the global top-K reservoir;
 *                              the only signal that can RED on
 *                              its own (α ≤ 1 → mean doesn't
 *                              exist for a stationary measure,
 *                              moments are the wrong primitive)
 *  11. jb_normality           — Jarque-Bera (1980) test against
 *                              the Gaussian; p = exp(−JB/2) under
 *                              χ²(2). Bands H₀ rejection.
 *  12. edgeworth_pdf_consistency — minimum of the truncated-
 *                              Edgeworth correction factor on a
 *                              standardized z-grid. Negative
 *                              minimum ⇒ moment-implied PDF is
 *                              not a valid density.
 *
 * Project history note (2026-04-25): A KS goodness-of-fit signal
 * built on a per-CPU log2-spaced histogram was prototyped and
 * rejected. The histogram violated the moments-only project
 * identity (and separately blew the BPF verifier budget on 6.12).
 * jb_normality and edgeworth_pdf_consistency cover the
 * "is this moment sequence Gaussian-like / does it imply a valid
 * PDF" questions natively from m1..m4, with no in-kernel buffer.
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
#include "iomoments_spectral.h"
#include "iomoments_topk.h"
#include "pebay.h"

enum iomoments_verdict_status {
	IOMOMENTS_VERDICT_GREEN = 0,
	IOMOMENTS_VERDICT_YELLOW = 1,
	IOMOMENTS_VERDICT_AMBER = 2,
	IOMOMENTS_VERDICT_RED = 3,
};

#define IOMOMENTS_VERDICT_RATIONALE_MAX 192
#define IOMOMENTS_VERDICT_MAX_SIGNALS 12

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

/*
 * Hankel matrix conditioning (Curto-Fialkow 1991).
 *
 * For a positive measure on R, the Hankel matrix built on the
 * moment sequence must be positive semi-definite. Its rank gives
 * a lower bound on the number of atoms supporting the measure:
 * rank-1 = single atom (degenerate), rank-2 = two atoms,
 * rank ≥ 3 = supports continuous distributions. A near-singular
 * (small-determinant) Hankel signals the moment sequence is
 * "close to" being supported on fewer atoms — moments above
 * order 2k can't distinguish atomic from continuous in that
 * regime.
 *
 * We have central moments to k=4, so we form the 3×3 central
 * Hankel:
 *
 *   H = [[ 1,   0,   μ₂ ],
 *        [ 0,   μ₂,  μ₃ ],
 *        [ μ₂,  μ₃,  μ₄ ]]
 *
 * with central moments μ_k = m_k / n. Its determinant expands to:
 *
 *   det(H) = μ₂·μ₄ − μ₃² − μ₂³
 *
 * Normalize by μ₂³ to get a dimensionless conditioning number:
 *
 *   κ = det(H) / μ₂³  =  (μ₄/μ₂² − 1) − (μ₃/μ₂^{3/2})²
 *                     =  (excess kurtosis + 2) − skewness²
 *
 * Reference (analytic):
 *   Gaussian:    κ = 3 − 1 − 0 = 2.0
 *   exponential: κ = 9 − 1 − 4 = 4.0
 *   uniform:     κ = 1.8 − 1 − 0 = 0.8
 *   ±1 two-atom: κ = 1 − 1 − 0 = 0  (rank-deficient by construction)
 *   log-normal:  κ → ∞ (pathologically large) for heavy tail
 *
 * Bands:
 *   κ > 0.5         → GREEN  (well-conditioned, supports continuous)
 *   0.1 .. 0.5      → YELLOW (constrained shape, e.g. uniform-ish)
 *   ≤ 0.1 (or < 0)  → AMBER  (near-rank-deficient, atomic-like)
 *   κ > 100         → AMBER  (pathologically large, log-normal-ish
 *                              moment sequence — moment-indeterminate)
 *
 * Doesn't go RED on its own — the kurtosis_sanity signal already
 * RED-flags degenerate spikes; carleman_partial_sum covers
 * heavy-tail indeterminacy from another angle.
 */
static inline void
iomoments_verdict_eval_hankel(const struct iomoments_summary *global,
			      struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (global->n == 0 || global->m2 <= 0.0) {
		snprintf(r, sizeof(r),
			 "n=%lu, m_2=%.3g; cannot evaluate Hankel",
			 (unsigned long)global->n, global->m2);
		iomoments_verdict_push(v, "hankel_conditioning",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double n = (double)global->n;
	double mu2 = global->m2 / n;
	double mu3 = global->m3 / n;
	double mu4 = global->m4 / n;
	double mu2_cubed = mu2 * mu2 * mu2;
	if (mu2_cubed <= 0.0) {
		snprintf(r, sizeof(r), "μ_2³ ≤ 0; cannot normalize");
		iomoments_verdict_push(v, "hankel_conditioning",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double det = mu2 * mu4 - mu3 * mu3 - mu2_cubed;
	double kappa = det / mu2_cubed;
	enum iomoments_verdict_status status;
	if (kappa > 100.0) {
		status = IOMOMENTS_VERDICT_AMBER;
	} else if (kappa > 0.5) {
		status = IOMOMENTS_VERDICT_GREEN;
	} else if (kappa > 0.1) {
		status = IOMOMENTS_VERDICT_YELLOW;
	} else {
		status = IOMOMENTS_VERDICT_AMBER;
	}
	snprintf(r, sizeof(r),
		 "det(H₃)/μ₂³ = %.3f"
		 " (Gaussian≈2.0, exp≈4.0; ≤0.1 atomic, >100 log-normal-ish)",
		 kappa);
	iomoments_verdict_push(v, "hankel_conditioning", status, r);
}

/*
 * Jarque-Bera (1980) normality test.
 *
 *   JB = n/6 · ( γ₁² + γ₂²/4 )
 *
 * where γ₁ = skewness and γ₂ = excess kurtosis. Under H₀ (sample
 * drawn from a Gaussian), JB → χ²(2) asymptotically. The χ²(2)
 * survival function is conveniently exp(−x/2), giving:
 *
 *   p = exp(−JB / 2)
 *
 * Bands:
 *   p > 0.05    → GREEN  (consistent with Gaussian — no rejection)
 *   0.001..0.05 → YELLOW (mild non-normality; moments are still a
 *                          reasonable summary but not a Gaussian one)
 *   p ≤ 0.001   → AMBER  (strongly non-Gaussian; quoting Gaussian-
 *                          interpretation statistics like Cornish-
 *                          Fisher quantiles is unsafe)
 *
 * The asymptotic distribution requires n ≳ 30 to be trustworthy;
 * below n = 8 we emit YELLOW with an insufficient-data rationale
 * rather than report a misleading p-value.
 *
 * Note: this signal answers a *different* question than the
 * other moments-self-consistency signals. Hankel/Carleman ask
 * "is the moment sequence valid / determinate?"; Hill asks "is
 * the tail too heavy for a finite-mean distribution?"; JB asks
 * "are the moments specifically *Gaussian-like*?" A workload can
 * score GREEN on the first three (well-conditioned, light-tailed,
 * determinate) yet AMBER on JB (e.g., tightly clustered around two
 * modes — moments exist and are well-behaved, but Gaussian-
 * interpretation breaks).
 */
static inline void
iomoments_verdict_eval_jb(const struct iomoments_summary *global,
			  struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (global->n < 8 || global->m2 <= 0.0) {
		snprintf(r, sizeof(r),
			 "n=%lu, m_2=%.3g; below JB asymptotic-validity floor",
			 (unsigned long)global->n, global->m2);
		iomoments_verdict_push(v, "jb_normality",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double skew = iomoments_summary_skewness(global);
	double exkurt = iomoments_summary_excess_kurtosis(global);
	double n = (double)global->n;
	double jb = n / 6.0 * (skew * skew + exkurt * exkurt / 4.0);
	double p = exp(-jb / 2.0);
	enum iomoments_verdict_status status;
	if (p > 0.05) {
		status = IOMOMENTS_VERDICT_GREEN;
	} else if (p > 0.001) {
		status = IOMOMENTS_VERDICT_YELLOW;
	} else {
		status = IOMOMENTS_VERDICT_AMBER;
	}
	snprintf(r, sizeof(r), "JB=%.2f, p=%.3g (γ₁=%+.2f, γ₂=%+.2f, n=%lu)",
		 jb, p, skew, exkurt, (unsigned long)global->n);
	iomoments_verdict_push(v, "jb_normality", status, r);
}

/*
 * Edgeworth-residual PDF-consistency signal.
 *
 * The Edgeworth series expresses a distribution near a Gaussian
 * reference using cumulants. Truncated at the m4-available terms:
 *
 *   f(z) ≈ φ(z) · [ 1
 *                + (γ₁/6)·H₃(z)
 *                + (γ₂/24)·H₄(z)
 *                + (γ₁²/72)·H₆(z) ]
 *
 * where φ(z) = standard normal PDF, H_n = probabilist's Hermite
 * polynomial of degree n:
 *
 *   H₃(z) = z³ − 3z
 *   H₄(z) = z⁴ − 6z² + 3
 *   H₆(z) = z⁶ − 15z⁴ + 45z² − 15
 *
 * φ(z) is positive everywhere; the bracketed factor is a
 * polynomial in z whose coefficients are functions of (γ₁, γ₂).
 * For Gaussian data γ₁ = γ₂ = 0, the bracket is identically 1,
 * the truncated PDF is exactly φ, and stays positive. As skewness
 * and kurtosis grow, the polynomial corrections can drive the
 * bracket negative on the tails. When that happens the moment-
 * truncated reconstruction is *not a valid probability density* —
 * a self-consistency violation distinct from the moment-sequence
 * realizability that Hankel checks.
 *
 * Implementation: evaluate the bracket on a grid of standardized
 * z values across [-5, 5] (about 1e-6 of total Gaussian mass
 * outside that band, irrelevant for the band decision), track
 * the minimum value.
 *
 * Bands (this signal is naturally binary — a PDF is either
 * everywhere-positive or it isn't):
 *
 *   min > 0   → GREEN  (truncated PDF is positive across the grid;
 *                        the moment-implied reconstruction is a
 *                        valid density)
 *   min ≤ 0   → AMBER  (truncated PDF goes negative somewhere; the
 *                        moment-implied reconstruction is *not* a
 *                        valid density — γ₁ and γ₂ are too extreme
 *                        for the m1/m2 baseline they sit on)
 *
 * YELLOW is reserved for insufficient-data (n < 8 or m₂ ≤ 0).
 *
 * Note on tails: at large |z| the Hermite polynomials grow fast;
 * the bracketed factor can drop well below 1 even on near-Gaussian
 * data. That's *not* a violation — factor ∈ (0, 1) just means the
 * moment-implied PDF is *smaller* than the Gaussian baseline at
 * those points, which is a perfectly valid density behavior. Only
 * factor ≤ 0 indicates an actual PDF-validity failure.
 *
 * Doesn't go RED on its own. Coverage of the genuinely-pathological
 * cases is split: Hill RED-flags too-heavy tail, kurtosis_sanity
 * RED-flags degenerate spike. Edgeworth's contribution is a
 * specifically-PDF-reconstruction-aware AMBER for "the moments
 * disagree with each other to the point of producing an invalid
 * PDF when reconstructed."
 */
static inline void
iomoments_verdict_eval_edgeworth(const struct iomoments_summary *global,
				 struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (global->n < 8 || global->m2 <= 0.0) {
		snprintf(r, sizeof(r),
			 "n=%lu, m_2=%.3g; cannot standardize for Edgeworth",
			 (unsigned long)global->n, global->m2);
		iomoments_verdict_push(v, "edgeworth_pdf_consistency",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double skew = iomoments_summary_skewness(global);
	double exkurt = iomoments_summary_excess_kurtosis(global);
	double min_factor = 1.0;
	double z_at_min = 0.0;
	/* 201-point grid across [-5, 5] — fine enough that the polynomial
	 * factor's minimum is captured to <1% even at high γ₁, γ₂ where
	 * the polynomial varies fastest. */
	for (int i = 0; i <= 200; i++) {
		double z = -5.0 + 0.05 * (double)i;
		double z2 = z * z;
		double z3 = z2 * z;
		double z4 = z2 * z2;
		double z6 = z4 * z2;
		double h3 = z3 - 3.0 * z;
		double h4 = z4 - 6.0 * z2 + 3.0;
		double h6 = z6 - 15.0 * z4 + 45.0 * z2 - 15.0;
		double factor = 1.0 + skew / 6.0 * h3 + exkurt / 24.0 * h4 +
				skew * skew / 72.0 * h6;
		if (factor < min_factor) {
			min_factor = factor;
			z_at_min = z;
		}
	}
	enum iomoments_verdict_status status =
		min_factor > 0.0 ? IOMOMENTS_VERDICT_GREEN
				 : IOMOMENTS_VERDICT_AMBER;
	snprintf(r, sizeof(r),
		 "Edgeworth min factor %.3f at z=%+.2f (γ₁=%+.2f, γ₂=%+.2f)",
		 min_factor, z_at_min, skew, exkurt);
	iomoments_verdict_push(v, "edgeworth_pdf_consistency", status, r);
}

/*
 * Spectral-flatness sweep signal. Reads the precomputed
 * iomoments_spectral_result (see iomoments_spectral.h) and emits
 * GREEN/YELLOW/AMBER based on the minimum variance ratio across
 * the sweep. The W' at which the minimum occurs is the suspected
 * aliasing period.
 *
 * Bands match the per-base nyquist_confidence signal's
 * sensibility — low ratio means windowed mean became phase-
 * insensitive, indicating periodic content the simple moments
 * average over rather than capture:
 *
 *   min ratio > 0.5  → GREEN  (smooth across scales)
 *   0.2 .. 0.5       → YELLOW (mild structure at some scale)
 *   ≤ 0.2            → AMBER  (clear aliasing at the W' of min)
 */
static inline void
iomoments_verdict_eval_spectral(const struct iomoments_spectral_result *spec,
				struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (spec->insufficient_data || spec->n_points == 0) {
		snprintf(r, sizeof(r),
			 "insufficient windows for spectral sweep");
		iomoments_verdict_push(v, "spectral_sweep",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double min_ratio = spec->min_ratio;
	double w_at_min = spec->points[spec->min_ratio_idx].window_seconds;
	enum iomoments_verdict_status status;
	if (min_ratio < 0.2) {
		status = IOMOMENTS_VERDICT_AMBER;
	} else if (min_ratio < 0.5) {
		status = IOMOMENTS_VERDICT_YELLOW;
	} else {
		status = IOMOMENTS_VERDICT_GREEN;
	}
	const char *interpretation =
		status == IOMOMENTS_VERDICT_AMBER    ? "aliasing suspected at "
						       "this period"
		: status == IOMOMENTS_VERDICT_YELLOW ? "mild periodic "
						       "structure at this scale"
						     : "smooth across scales";
	snprintf(r, sizeof(r), "min ratio %.3f at W' = %.4f s; %s", min_ratio,
		 w_at_min, interpretation);
	iomoments_verdict_push(v, "spectral_sweep", status, r);
}

/*
 * Hill (1975) tail-index signal. Reads the global top-K reservoir
 * built by merging every window's per-window top-K, computes the
 * Hill estimator, bands the result.
 *
 * α > 2.5  → GREEN  (light tail; M2..M4 estimators well-defined)
 * α in (2.0, 2.5] → YELLOW (kurtosis-edge; estimator noisy)
 * α in (1.0, 2.0] → AMBER (heavy tail; kurtosis doesn't exist;
 *                          variance does)
 * α ≤ 1.0  → RED    (very heavy tail; mean doesn't exist for a
 *                    stationary measure → moments are the wrong
 *                    primitive)
 *
 * α̂ = 0 (cannot evaluate; reservoir empty or single-entry) →
 * YELLOW with insufficient-data rationale.
 *
 * This is the only signal that can drive RED on a continuous
 * distribution — the others (variance_sanity, kurtosis_sanity,
 * sample_count) RED only on degenerate inputs (constant stream,
 * spike, too few samples). Without Hill the heavy-tailed-but-
 * continuous case slips through.
 */
static inline void
iomoments_verdict_eval_hill(const struct iomoments_topk *global_topk,
			    struct iomoments_verdict *v)
{
	char r[IOMOMENTS_VERDICT_RATIONALE_MAX];
	if (global_topk->count < 2) {
		snprintf(r, sizeof(r),
			 "top-K count = %u < 2; insufficient for Hill",
			 global_topk->count);
		iomoments_verdict_push(v, "hill_tail_index",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	double alpha = iomoments_hill_estimator(global_topk);
	if (alpha == 0.0) {
		snprintf(r, sizeof(r),
			 "Hill estimator α = 0 (degenerate top-K)");
		iomoments_verdict_push(v, "hill_tail_index",
				       IOMOMENTS_VERDICT_YELLOW, r);
		return;
	}
	enum iomoments_verdict_status status;
	const char *interpretation;
	if (alpha <= 1.0) {
		status = IOMOMENTS_VERDICT_RED;
		interpretation = "mean doesn't exist; moments wrong primitive";
	} else if (alpha <= 2.0) {
		status = IOMOMENTS_VERDICT_AMBER;
		interpretation = "heavy tail; kurtosis non-existent";
	} else if (alpha <= 2.5) {
		status = IOMOMENTS_VERDICT_YELLOW;
		interpretation = "kurtosis-edge; estimator noisy";
	} else {
		status = IOMOMENTS_VERDICT_GREEN;
		interpretation = "light tail";
	}
	snprintf(r, sizeof(r), "α̂ = %.3f (k=%u); %s", alpha, global_topk->count,
		 interpretation);
	iomoments_verdict_push(v, "hill_tail_index", status, r);
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
			  const struct iomoments_spectral_result *spec,
			  struct iomoments_verdict *out)
{
	memset(out, 0, sizeof(*out));
	out->overall = IOMOMENTS_VERDICT_GREEN;

	/* Aggregate every window's top-K into a single global top-K. */
	struct iomoments_topk global_topk;
	iomoments_topk_init(&global_topk);
	for (size_t i = 0; i < count; i++) {
		iomoments_topk_merge(&global_topk, &ring[i].topk);
	}

	iomoments_verdict_eval_sample_count(global, out);
	iomoments_verdict_eval_variance_sanity(global, out);
	iomoments_verdict_eval_kurtosis_sanity(global, out);
	iomoments_verdict_eval_carleman(global, out);
	iomoments_verdict_eval_hankel(global, out);
	iomoments_verdict_eval_jb(global, out);
	iomoments_verdict_eval_edgeworth(global, out);
	iomoments_verdict_eval_hill(&global_topk, out);
	iomoments_verdict_eval_nyquist(l2, out);
	iomoments_verdict_eval_autocorr(l2, out);
	iomoments_verdict_eval_spectral(spec, out);
	iomoments_verdict_eval_half_split(ring, count, out);
}

#endif /* IOMOMENTS_VERDICT_H */
