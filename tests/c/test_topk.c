/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Tests for src/iomoments_topk.h — reservoir invariants and the
 * Hill (1975) tail-index estimator.
 *
 * Reservoir tests pin the structural property: after N inserts of
 * arbitrary values, the K largest survive. Hill tests pin the
 * estimator's calibration: Pareto(α) samples produce α̂ ≈ α to
 * within the standard-error band of α/√k.
 *
 * Exit code: 0 = all pass; 1 = at least one failure.
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../src/iomoments_topk.h"

static int failures;

#define CHECK(cond)                                                            \
	do {                                                                   \
		if (!(cond)) {                                                 \
			fprintf(stderr, "FAIL %s:%d  %s\n", __FILE__,          \
				__LINE__, #cond);                              \
			failures += 1;                                         \
		}                                                              \
	} while (0)

/* Deterministic LCG for reproducible test data. */
static iomoments_u64 lcg_next(iomoments_u64 *state)
{
	*state = (*state) * 6364136223846793005ULL + 1442695040888963407ULL;
	return *state;
}

static double lcg_uniform(iomoments_u64 *state)
{
	iomoments_u64 u53 = lcg_next(state) >> 11;
	double u = (double)u53 / (double)(1ULL << 53);
	if (u <= 0.0)
		u = 1e-12;
	return u;
}

/*
 * Pareto(α) sample with x_min = 1: X = 1 / U^(1/α).
 * Tail Pr(X > x) = x^(-α). Hill estimator's calibration target.
 */
static double pareto_sample(double alpha, iomoments_u64 *state)
{
	double u = lcg_uniform(state);
	return pow(u, -1.0 / alpha);
}

/* --- Reservoir invariants ------------------------------------------- */

static void test_init_zero(void)
{
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	CHECK(t.count == 0);
	CHECK(t.min_idx == 0);
}

static void test_insert_below_capacity_keeps_all(void)
{
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	for (iomoments_u32 i = 0; i < 5; i++) {
		iomoments_topk_insert(&t, (iomoments_u64)(i + 1) * 100);
	}
	CHECK(t.count == 5);
	/* All 5 inserted values present. */
	for (iomoments_u32 v = 1; v <= 5; v++) {
		int found = 0;
		for (iomoments_u32 i = 0; i < t.count; i++) {
			if (t.samples[i] == v * 100)
				found = 1;
		}
		CHECK(found);
	}
}

static void test_insert_at_capacity_keeps_top_k(void)
{
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	/* Fill with 1..K. min should land on 1. */
	for (iomoments_u32 i = 1; i <= IOMOMENTS_TOPK_K; i++) {
		iomoments_topk_insert(&t, i);
	}
	CHECK(t.count == IOMOMENTS_TOPK_K);
	CHECK(t.samples[t.min_idx] == 1);
	/* Insert a value larger than 1 — replaces 1; new min becomes 2. */
	iomoments_topk_insert(&t, 1000);
	CHECK(t.samples[t.min_idx] == 2);
	/* Insert smaller than current min — no-op. */
	iomoments_topk_insert(&t, 1);
	CHECK(t.samples[t.min_idx] == 2);
	/* Final reservoir contents: {2..K, 1000}. */
	int found_1000 = 0;
	int found_1 = 0;
	for (iomoments_u32 i = 0; i < IOMOMENTS_TOPK_K; i++) {
		if (t.samples[i] == 1000)
			found_1000 = 1;
		if (t.samples[i] == 1)
			found_1 = 1;
	}
	CHECK(found_1000);
	CHECK(!found_1);
}

static void test_insert_random_keeps_top_k(void)
{
	/* Insert 10000 random samples in [1, 100000]; reservoir should
	 * end up holding the K largest values. */
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	iomoments_u64 state = 0xCAFEDEADBEEFULL;
	const size_t N = 10000;
	iomoments_u64 *all = calloc(N, sizeof(*all));
	if (!all) {
		fprintf(stderr, "calloc\n");
		failures += 1;
		return;
	}
	for (size_t i = 0; i < N; i++) {
		all[i] = (lcg_next(&state) % 100000) + 1;
		iomoments_topk_insert(&t, all[i]);
	}
	/* Sort all descending; the top-K should match the reservoir
	 * (modulo ordering — both are unordered sets here). */
	for (size_t i = 1; i < N; i++) {
		iomoments_u64 x = all[i];
		size_t j = i;
		while (j > 0 && all[j - 1] < x) {
			all[j] = all[j - 1];
			j -= 1;
		}
		all[j] = x;
	}
	/* Membership check: every reservoir element appears in the top-K
	 * of the full stream. (Allowing for ties at the K-th boundary.) */
	for (iomoments_u32 i = 0; i < t.count; i++) {
		int found = 0;
		for (size_t j = 0; j < IOMOMENTS_TOPK_K; j++) {
			if (all[j] == t.samples[i]) {
				found = 1;
				break;
			}
		}
		CHECK(found);
	}
	free(all);
}

static void test_merge_keeps_top_k_of_union(void)
{
	struct iomoments_topk a, b, m;
	iomoments_topk_init(&a);
	iomoments_topk_init(&b);
	iomoments_topk_init(&m);
	/* a: 1..K. b: K+1..2K. Merge into m via inserting union should
	 * yield {K+1..2K} (the top-K of the union). */
	for (iomoments_u32 i = 1; i <= IOMOMENTS_TOPK_K; i++) {
		iomoments_topk_insert(&a, i);
		iomoments_topk_insert(&b, i + IOMOMENTS_TOPK_K);
	}
	iomoments_topk_merge(&m, &a);
	iomoments_topk_merge(&m, &b);
	CHECK(m.count == IOMOMENTS_TOPK_K);
	/* All of {K+1..2K} should be present; none of {1..K}. */
	for (iomoments_u32 v = 1; v <= IOMOMENTS_TOPK_K; v++) {
		int found_low = 0;
		for (iomoments_u32 i = 0; i < m.count; i++) {
			if (m.samples[i] == v)
				found_low = 1;
		}
		CHECK(!found_low);
	}
	for (iomoments_u32 v = IOMOMENTS_TOPK_K + 1; v <= 2 * IOMOMENTS_TOPK_K;
	     v++) {
		int found_high = 0;
		for (iomoments_u32 i = 0; i < m.count; i++) {
			if (m.samples[i] == v)
				found_high = 1;
		}
		CHECK(found_high);
	}
}

/* --- Hill estimator calibration on Pareto(α) -------------------------- */

static void test_hill_pareto_alpha_2(void)
{
	/* Pareto(α=2) → Hill estimate should be ≈ 2 within standard
	 * error α/√k. With k=32 that's ~0.35; require |α̂ - 2| < 0.6. */
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	iomoments_u64 state = 0x123456789ABCDEFULL;
	for (size_t i = 0; i < 10000; i++) {
		double sample = pareto_sample(2.0, &state);
		/* Quantize to integer ns-scale so reservoir holds u64. */
		iomoments_u64 q = (iomoments_u64)(sample * 1e6);
		iomoments_topk_insert(&t, q);
	}
	double alpha = iomoments_hill_estimator(&t);
	CHECK(alpha > 1.4 && alpha < 2.6);
}

static void test_hill_pareto_alpha_1(void)
{
	/* Pareto(α=1) → Hill ≈ 1 ± standard error 1/√32 ≈ 0.18.
	 * Require |α̂ - 1| < 0.4. */
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	iomoments_u64 state = 0xFEEDFACECAFEF00DULL;
	for (size_t i = 0; i < 10000; i++) {
		double sample = pareto_sample(1.0, &state);
		iomoments_u64 q = (iomoments_u64)(sample * 1e6);
		if (q == 0)
			q = 1;
		iomoments_topk_insert(&t, q);
	}
	double alpha = iomoments_hill_estimator(&t);
	CHECK(alpha > 0.6 && alpha < 1.4);
}

static void test_hill_pareto_alpha_3_5(void)
{
	/* Pareto(α=3.5) — the GREEN-band light tail. Standard error is
	 * 3.5/√32 ≈ 0.62; require α̂ > 2.5 (above YELLOW threshold). */
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	iomoments_u64 state = 0xABCDEF0123456789ULL;
	for (size_t i = 0; i < 10000; i++) {
		double sample = pareto_sample(3.5, &state);
		iomoments_u64 q = (iomoments_u64)(sample * 1e6);
		if (q == 0)
			q = 1;
		iomoments_topk_insert(&t, q);
	}
	double alpha = iomoments_hill_estimator(&t);
	CHECK(alpha > 2.5);
}

static void test_hill_returns_zero_on_empty(void)
{
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	CHECK(iomoments_hill_estimator(&t) == 0.0);
}

static void test_hill_returns_zero_on_single_entry(void)
{
	struct iomoments_topk t;
	iomoments_topk_init(&t);
	iomoments_topk_insert(&t, 1000);
	CHECK(iomoments_hill_estimator(&t) == 0.0);
}

int main(void)
{
	test_init_zero();
	test_insert_below_capacity_keeps_all();
	test_insert_at_capacity_keeps_top_k();
	test_insert_random_keeps_top_k();
	test_merge_keeps_top_k_of_union();
	test_hill_pareto_alpha_2();
	test_hill_pareto_alpha_1();
	test_hill_pareto_alpha_3_5();
	test_hill_returns_zero_on_empty();
	test_hill_returns_zero_on_single_entry();

	if (failures > 0) {
		fprintf(stderr, "\n%d assertion(s) failed.\n", failures);
		return 1;
	}
	printf("All top-K + Hill tail-index tests passed.\n");
	return 0;
}
