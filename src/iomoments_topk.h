/* SPDX-License-Identifier: GPL-2.0-only OR AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * iomoments top-K reservoir (Hill 1975 tail-index input).
 *
 * Maintains the K largest samples seen, kept unsorted for O(K)
 * insert. The Hill estimator reads the reservoir at verdict-compute
 * time, sorts descending, and computes:
 *
 *   α̂ = 1 / ( (1/(K-1)) · Σ_{i=1..K-1} ln(X_(i) / X_(K)) )
 *
 * where X_(1) ≥ X_(2) ≥ ... ≥ X_(K) are the order statistics.
 *
 * For our deployment shape: a per-CPU instance lives alongside the
 * iomoments_summary_bpf value in a parallel BPF per-CPU map; on
 * each block I/O completion the latency is fed to both the summary
 * (Pébay update) and the top-K reservoir (insert if larger than
 * current minimum). Userspace drains all CPUs each window, merges
 * via union-and-truncate-to-K, stores the merged top-K in the
 * iomoments_window snapshot. Verdict computation merges every
 * window's top-K into a global top-K and evaluates Hill on that.
 *
 * K = 32 chosen as the sweet spot:
 *   - small enough to keep BPF map value compact (~256 bytes)
 *   - small enough to keep the per-insert O(K) loop verifier-cheap
 *   - large enough for a meaningful Hill estimate (Hill estimator
 *     standard error scales as α/√k; k=32 gives ~17% relative
 *     error at α=1.0, ~30% at α=3.0)
 *
 * Header structure follows pebay_bpf.h's split — primitives that
 * compile under -target bpf are above the `__bpf__` gate;
 * floating-point Hill estimator is userspace-only.
 *
 * Dual-licensed `(GPL-2.0-only OR AGPL-3.0-or-later)`: the BPF
 * portion of this header is included from src/iomoments.bpf.c and
 * needs the GPL-compatible label for the kernel verifier.
 */

#ifndef IOMOMENTS_TOPK_H
#define IOMOMENTS_TOPK_H

#include "u128.h" /* iomoments_u64, IOMOMENTS_BPF_INLINE */

#define IOMOMENTS_TOPK_K 32

typedef unsigned int iomoments_u32;

struct iomoments_topk {
	/*
	 * samples[0..count-1] hold the K largest seen so far. Order
	 * within the array is arbitrary (insertion-order, not sorted)
	 * — sort on read at verdict time. Once count == K, min_idx
	 * tracks the smallest entry's slot for O(1) reject decision.
	 */
	iomoments_u64 samples[IOMOMENTS_TOPK_K];
	iomoments_u32 count;   /* ≤ IOMOMENTS_TOPK_K */
	iomoments_u32 min_idx; /* valid when count == K */
};

#define IOMOMENTS_TOPK_ZERO                                                    \
	{                                                                      \
		{0}, 0, 0                                                      \
	}

IOMOMENTS_BPF_INLINE void iomoments_topk_init(struct iomoments_topk *t)
{
	for (iomoments_u32 i = 0; i < IOMOMENTS_TOPK_K; i++) {
		t->samples[i] = 0;
	}
	t->count = 0;
	t->min_idx = 0;
}

/*
 * Recompute min_idx by linear scan. O(K). Internal helper used by
 * insert when the reservoir is at capacity and a new sample
 * displaces the current min.
 */
IOMOMENTS_BPF_INLINE void iomoments_topk_recompute_min(struct iomoments_topk *t)
{
	iomoments_u32 min_i = 0;
	for (iomoments_u32 i = 1; i < IOMOMENTS_TOPK_K; i++) {
		if (t->samples[i] < t->samples[min_i]) {
			min_i = i;
		}
	}
	t->min_idx = min_i;
}

/*
 * Insert x into the reservoir, keeping only the K largest seen.
 * O(K) when at capacity (linear scan to find new min); O(1)
 * amortized after warm-up because most samples don't beat the
 * current min and short-circuit out.
 */
IOMOMENTS_BPF_INLINE void iomoments_topk_insert(struct iomoments_topk *t,
						iomoments_u64 x)
{
	if (t->count < IOMOMENTS_TOPK_K) {
		t->samples[t->count] = x;
		t->count += 1;
		if (t->count == IOMOMENTS_TOPK_K) {
			iomoments_topk_recompute_min(t);
		}
		return;
	}
	if (x <= t->samples[t->min_idx]) {
		return;
	}
	t->samples[t->min_idx] = x;
	iomoments_topk_recompute_min(t);
}

/*
 * Merge src into dst, keeping the top-K of the union. O(K)
 * amortized since most src samples won't beat dst's current min.
 */
IOMOMENTS_BPF_INLINE void iomoments_topk_merge(struct iomoments_topk *dst,
					       const struct iomoments_topk *src)
{
	for (iomoments_u32 i = 0; i < src->count; i++) {
		iomoments_topk_insert(dst, src->samples[i]);
	}
}

/* --- Userspace-only readout: Hill estimator -------------------------- */

#if !defined(__bpf__)

#include <math.h>
#include <stddef.h>

/*
 * Sort an iomoments_u64 array in descending order. Insertion sort,
 * O(n²) worst case but with n ≤ K = 32 this is trivial and avoids
 * the libc qsort callback dance.
 */
static inline void iomoments_topk_sort_descending(iomoments_u64 *arr, size_t n)
{
	for (size_t i = 1; i < n; i++) {
		iomoments_u64 x = arr[i];
		size_t j = i;
		while (j > 0 && arr[j - 1] < x) {
			arr[j] = arr[j - 1];
			j -= 1;
		}
		arr[j] = x;
	}
}

/*
 * Hill (1975) tail-index estimator over the K largest samples in
 * the reservoir.
 *
 *   α̂ = 1 / ( (1/(k-1)) · Σ_{i=1..k-1} ln(X_(i) / X_(k)) )
 *
 * Returns 0.0 when the reservoir has fewer than 2 entries or when
 * X_(K) is non-positive (can't take a log). Caller treats α̂ = 0
 * as "cannot evaluate" and emits the YELLOW band.
 *
 * Reference α values:
 *   exponential, half-Gaussian, any thin tail: α → ∞
 *   Pareto(α): tail-index is α exactly (the calibration target)
 *   log-normal: α → ∞ (subexponential — Hill underestimates)
 *   Cauchy: α = 1
 *   stable(α): tail-index is α
 */
static inline double iomoments_hill_estimator(const struct iomoments_topk *t)
{
	if (t->count < 2) {
		return 0.0;
	}
	iomoments_u64 sorted[IOMOMENTS_TOPK_K];
	size_t n = t->count;
	for (size_t i = 0; i < n; i++) {
		sorted[i] = t->samples[i];
	}
	iomoments_topk_sort_descending(sorted, n);
	if (sorted[n - 1] == 0) {
		return 0.0;
	}
	double xk = (double)sorted[n - 1];
	double sum = 0.0;
	for (size_t i = 0; i < n - 1; i++) {
		sum += log((double)sorted[i] / xk);
	}
	if (sum <= 0.0) {
		return 0.0;
	}
	double mean = sum / (double)(n - 1);
	return 1.0 / mean;
}

#endif /* !__bpf__ */

#endif /* IOMOMENTS_TOPK_H */
