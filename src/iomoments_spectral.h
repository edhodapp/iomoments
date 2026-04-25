/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * iomoments spectral-flatness sweep (D013, D007).
 *
 * Extends the Level 2 Nyquist diagnostic from a single
 * variance-ratio at the base window length W to a sweep across
 * window lengths W' = k·W for k in {1, 2, 4, 8, ...}. For each
 * sweep point, re-aggregates every k consecutive base-window
 * snapshots into a virtual window, computes Var(m1_virtual)
 * across virtuals, and compares to the CLT prediction
 * σ²_global / n_per_virtual.
 *
 * Why sweep: the per-base-window Nyquist confidence catches
 * aliasing when the hidden period matches W. But periodic
 * structure at periods 2·W, 4·W, 8·W is invisible to a single-
 * scale ratio — the windowed mean at base W still varies with
 * the periodicity, so var_obs ≈ var_pred at base W. Re-binning
 * to W' = k·W exposes the dip when k·W matches the hidden
 * period: at that k, the virtual window straddles whole periods,
 * the windowed mean becomes phase-insensitive, var_obs collapses.
 *
 * The sweep produces a list of (W', ratio) pairs and the minimum
 * ratio across the sweep. The verdict-signal evaluator
 * (in iomoments_verdict.h) reads the minimum ratio and the W'
 * at which it occurred, reports the suspected aliasing period.
 *
 * Userspace-only, header-only by convention. Reuses the existing
 * pebay.h primitives — no new infrastructure.
 */

#ifndef IOMOMENTS_SPECTRAL_H
#define IOMOMENTS_SPECTRAL_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "iomoments_level2.h"
#include "pebay.h"

/*
 * Maximum number of sweep points. With doubling k (1, 2, 4, ...),
 * 8 points covers k up to 128. For deployments with thousands of
 * base windows that's plenty; for the small-N case the sweep
 * stops early when fewer than 4 virtual windows would be
 * available at that k.
 */
#define IOMOMENTS_SPECTRAL_MAX_POINTS 8

struct iomoments_spectral_point {
	size_t k;	       /* virtual window = k base windows */
	double window_seconds; /* k · base_window_seconds */
	size_t n_virtual_windows;
	double var_observed;	  /* Var(m1_virtual) across virtuals */
	double var_predicted_clt; /* σ²_global / n_per_virtual */
	double ratio;		  /* var_observed / var_predicted_clt */
};

struct iomoments_spectral_result {
	size_t n_points;
	struct iomoments_spectral_point points[IOMOMENTS_SPECTRAL_MAX_POINTS];
	double min_ratio;     /* min across points; INFINITY if none */
	size_t min_ratio_idx; /* index into points; SIZE_MAX if none */
	int insufficient_data;
};

/*
 * Run the spectral-flatness sweep. Each k in {1, 2, 4, ...} that
 * still yields ≥ 4 virtual windows from the input ring contributes
 * one sweep point; the sweep stops when the next k would leave
 * fewer than 4 virtuals.
 */
static inline void
iomoments_spectral_sweep(const struct iomoments_window *ring, size_t count,
			 const struct iomoments_summary *global,
			 double base_window_seconds,
			 struct iomoments_spectral_result *out)
{
	memset(out, 0, sizeof(*out));
	out->min_ratio = (double)INFINITY;
	out->min_ratio_idx = SIZE_MAX;

	if (count < 4) {
		out->insufficient_data = 1;
		return;
	}
	double global_var = iomoments_summary_variance(global);
	if (global_var <= 0.0) {
		out->insufficient_data = 1;
		return;
	}

	for (size_t k = 1;
	     k <= count / 4 && out->n_points < IOMOMENTS_SPECTRAL_MAX_POINTS;
	     k *= 2) {
		struct iomoments_summary m1_dist = IOMOMENTS_SUMMARY_ZERO;
		size_t n_virtual = 0;
		uint64_t total_samples = 0;
		for (size_t i = 0; i + k <= count; i += k) {
			struct iomoments_summary virtual_summary =
				IOMOMENTS_SUMMARY_ZERO;
			for (size_t j = 0; j < k; j++) {
				iomoments_summary_merge(&virtual_summary,
							&ring[i + j].summary);
			}
			if (virtual_summary.n > 0) {
				iomoments_summary_update(&m1_dist,
							 virtual_summary.m1);
				total_samples += virtual_summary.n;
				n_virtual += 1;
			}
		}
		if (n_virtual < 4) {
			break;
		}
		struct iomoments_spectral_point *p =
			&out->points[out->n_points];
		p->k = k;
		p->window_seconds = (double)k * base_window_seconds;
		p->n_virtual_windows = n_virtual;
		p->var_observed = iomoments_summary_variance(&m1_dist);
		double avg_n_per_virtual =
			(double)total_samples / (double)n_virtual;
		if (avg_n_per_virtual > 0.0) {
			p->var_predicted_clt = global_var / avg_n_per_virtual;
		} else {
			p->var_predicted_clt = 0.0;
		}
		if (p->var_predicted_clt > 0.0) {
			p->ratio = p->var_observed / p->var_predicted_clt;
		} else {
			p->ratio = 0.0;
		}
		if (p->ratio < out->min_ratio) {
			out->min_ratio = p->ratio;
			out->min_ratio_idx = out->n_points;
		}
		out->n_points += 1;
	}

	if (out->n_points == 0) {
		out->insufficient_data = 1;
	}
}

#endif /* IOMOMENTS_SPECTRAL_H */
