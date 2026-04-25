/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * _POSIX_C_SOURCE is needed to expose clock_gettime, clock_nanosleep,
 * CLOCK_MONOTONIC, and TIMER_ABSTIME under the strict -std=c11 build
 * flags. 200809L is the standard floor that includes all of these.
 * The leading-underscore name is *required* by POSIX itself —
 * clang-tidy's bugprone-reserved-identifier doesn't model the
 * feature-test-macro convention.
 */
/* NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp) */
#define _POSIX_C_SOURCE 200809L

/*
 * iomoments userspace loader + aggregator.
 *
 * Runs the iomoments BPF program attached to blk_mq_start_request /
 * blk_mq_end_request via fentry, periodically drains the per-CPU
 * fixed-point summaries into a time series of windowed snapshots,
 * and prints a shape report.
 *
 * Usage:
 *
 *     iomoments [--duration=<secs>] [--window=<ms>] [--help]
 *
 * Periodic-drain (D013 Level 1 → Level 2):
 *
 *   Every --window milliseconds, userspace reads the per-CPU map,
 *   merges into a single windowed iomoments_summary, resets the
 *   per-CPU accumulators to zero, and pushes the snapshot onto a
 *   time-indexed ring. The ring is the input to Level 2 moments-
 *   of-moments analysis (variance of windowed mean, lagged
 *   covariance, Nyquist confidence) which feeds the D007
 *   diagnostic-verdict layer.
 *
 * Today's scope:
 *
 *   - Loads build/iomoments.bpf.o via libbpf and attaches both
 *     BPF programs.
 *   - Periodically drains + resets the per-CPU map every
 *     --window ms; stores each snapshot in the windowed ring.
 *   - At end-of-duration, aggregates the ring via pebay.h's
 *     parallel-combine rule (Level 1 global moments).
 *   - Runs iomoments_level2_analyze on the ring (D013): variance
 *     of windowed mean vs CLT, autocorr at fixed lags, Nyquist
 *     confidence.
 *   - Runs iomoments_verdict_compute (D007): per-signal
 *     Green/Yellow/Amber/Red emission with rationale, plus a
 *     worst-of-all overall verdict.
 *   - Prints the moments report, the Level 2 statistics, and the
 *     verdict with per-signal breakdown.
 *
 * NOT yet in scope (follow-up):
 *
 *   - Hill tail-index estimator (needs order-statistic reservoir).
 *   - KS goodness-of-fit to log-normal (needs empirical CDF).
 *   - Device / workload-class segmentation.
 *
 * Drain race:
 *
 *   Between bpf_map_lookup_elem (read all CPUs) and the subsequent
 *   bpf_map_update_elem (zero all CPUs), any per-CPU update that
 *   fires on a third CPU lands in BPF's view of the map and is
 *   then overwritten by our zeroing. Practical loss: a few samples
 *   per drain at high IOPS. For Level 2 statistics this contributes
 *   a small constant-jitter to per-window sample count, well below
 *   the statistical noise of the moment estimators themselves.
 *   Lossless drain via map-swap is a follow-up if measurement
 *   shows it matters.
 */

#include <errno.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "iomoments_level2.h"
#include "iomoments_spectral.h"
#include "iomoments_verdict.h"
#include "pebay.h"
#include "pebay_bpf.h"

#define IOMOMENTS_BPF_OBJECT "build/iomoments.bpf.o"
#define IOMOMENTS_MAP_NAME "iomoments_summary"
#define IOMOMENTS_DEFAULT_DURATION 10
#define IOMOMENTS_DEFAULT_WINDOW_MS 100
#define IOMOMENTS_NSEC_PER_SEC 1000000000ULL
#define IOMOMENTS_NSEC_PER_MSEC 1000000ULL

static volatile sig_atomic_t stop_flag;

static void stop_handler(int signo)
{
	(void)signo;
	stop_flag = 1;
}

/*
 * Quiet libbpf's default logging down to errors — we produce our own
 * human-readable output. Anything libbpf logs as WARN/INFO about
 * missing BTF / kernel-feature probing is noise for a beta user.
 */
/*
 * vfprintf here forwards libbpf's own format strings verbatim —
 * libbpf constructs them, not us. The -Wformat-nonliteral diagnostic
 * is correct in the abstract but unavoidable for logging-sink
 * callbacks whose whole contract is "take a format string from the
 * caller."
 */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wformat-nonliteral"
static int libbpf_print_quiet(enum libbpf_print_level level, const char *fmt,
			      va_list args)
{
	if (level == LIBBPF_WARN) {
		vfprintf(stderr, fmt, args);
		return 0;
	}
	if (level > LIBBPF_WARN) {
		return 0;
	}
	return vfprintf(stderr, fmt, args);
}
#pragma GCC diagnostic pop

/*
 * Convert a single CPU's fixed-point summary into the double-precision
 * shape that pebay.h's merge expects. m1 comes from the Q32.32 readout;
 * m2 is a plain cast (int64 ns² → double); m3/m4 come from the s128
 * readout helper that handles two's-complement → double cleanly.
 */
static void bpf_summary_to_ref(const struct iomoments_summary_bpf *bpf,
			       struct iomoments_summary *ref)
{
	ref->n = bpf->n;
	ref->m1 = iomoments_summary_bpf_mean_ns(bpf);
	ref->m2 = (double)bpf->m2;
	ref->m3 = s128_to_double(bpf->m3);
	ref->m4 = s128_to_double(bpf->m4);
}

/*
 * Drain the per-CPU map into a single merged windowed summary, then
 * reset all per-CPU values to zero so the next window starts fresh.
 * See top-of-file note about the lookup→reset race; loss is bounded
 * to events that fire between the two map operations and is
 * acceptable for Level 2 diagnostics.
 */
static int drain_and_reset_summaries(int map_fd, int ncpu,
				     struct iomoments_summary *out)
{
	struct iomoments_summary_bpf *percpu_values =
		calloc((size_t)ncpu, sizeof(*percpu_values));
	if (!percpu_values) {
		perror("calloc percpu_values");
		return -ENOMEM;
	}

	__u32 key = 0;
	int rc = bpf_map_lookup_elem(map_fd, &key, percpu_values);
	if (rc) {
		fprintf(stderr, "bpf_map_lookup_elem: %s\n", strerror(errno));
		free(percpu_values);
		return rc;
	}

	struct iomoments_summary merged = IOMOMENTS_SUMMARY_ZERO;
	for (int cpu = 0; cpu < ncpu; cpu++) {
		struct iomoments_summary cpu_ref = IOMOMENTS_SUMMARY_ZERO;
		bpf_summary_to_ref(&percpu_values[cpu], &cpu_ref);
		iomoments_summary_merge(&merged, &cpu_ref);
	}
	*out = merged;

	memset(percpu_values, 0, (size_t)ncpu * sizeof(*percpu_values));
	rc = bpf_map_update_elem(map_fd, &key, percpu_values, BPF_ANY);
	free(percpu_values);
	if (rc) {
		fprintf(stderr, "bpf_map_update_elem (reset): %s\n",
			strerror(errno));
		return rc;
	}
	return 0;
}

/*
 * Add a window snapshot to a CLOCK_MONOTONIC-indexed ts. Helper for
 * the periodic-drain loop: takes a pre-computed wakeup timespec and
 * packs it into the u64 ns field on the snapshot.
 */
static uint64_t timespec_to_ns(const struct timespec *ts)
{
	return (uint64_t)ts->tv_sec * IOMOMENTS_NSEC_PER_SEC +
	       (uint64_t)ts->tv_nsec;
}

static void timespec_add_ns(struct timespec *ts, uint64_t ns)
{
	uint64_t total = timespec_to_ns(ts) + ns;
	ts->tv_sec = (time_t)(total / IOMOMENTS_NSEC_PER_SEC);
	ts->tv_nsec = (long)(total % IOMOMENTS_NSEC_PER_SEC);
}

/*
 * Periodic-drain main loop. Wakes every window_ms via absolute
 * clock_nanosleep, drains the per-CPU map into the next ring slot,
 * stops at duration or signal. Returns the count of windows captured
 * (including the final post-loop drain) or SIZE_MAX on hard error.
 */
static size_t run_drain_loop(int map_fd, int ncpu, int duration, int window_ms,
			     struct iomoments_window *ring,
			     size_t ring_capacity)
{
	uint64_t window_ns =
		(uint64_t)window_ms * (uint64_t)IOMOMENTS_NSEC_PER_MSEC;
	uint64_t total_ns =
		(uint64_t)duration * (uint64_t)IOMOMENTS_NSEC_PER_SEC;

	struct timespec start_ts;
	if (clock_gettime(CLOCK_MONOTONIC, &start_ts) != 0) {
		perror("clock_gettime");
		return SIZE_MAX;
	}
	struct timespec next_wakeup = start_ts;
	uint64_t start_ns = timespec_to_ns(&start_ts);
	size_t count = 0;

	while (!stop_flag) {
		timespec_add_ns(&next_wakeup, window_ns);
		if (timespec_to_ns(&next_wakeup) - start_ns > total_ns) {
			break;
		}
		int sleep_rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME,
					       &next_wakeup, NULL);
		if (sleep_rc == EINTR) {
			break;
		}
		if (sleep_rc != 0) {
			fprintf(stderr, "clock_nanosleep: %s\n",
				strerror(sleep_rc));
			break;
		}
		struct iomoments_summary win = IOMOMENTS_SUMMARY_ZERO;
		if (drain_and_reset_summaries(map_fd, ncpu, &win) != 0) {
			return SIZE_MAX;
		}
		if (count < ring_capacity) {
			ring[count].end_ts_ns = timespec_to_ns(&next_wakeup);
			ring[count].summary = win;
			count += 1;
		}
	}

	/* Final drain: capture samples since the last periodic drain. */
	if (count < ring_capacity) {
		struct iomoments_summary win = IOMOMENTS_SUMMARY_ZERO;
		if (drain_and_reset_summaries(map_fd, ncpu, &win) == 0) {
			struct timespec final_ts;
			clock_gettime(CLOCK_MONOTONIC, &final_ts);
			ring[count].end_ts_ns = timespec_to_ns(&final_ts);
			ring[count].summary = win;
			count += 1;
		}
	}
	return count;
}

/*
 * Print the user-facing report. Units: ns everywhere. Variance is
 * the population variance (σ², m2/n), per D006. Skewness and excess
 * kurtosis are dimensionless population moments (γ₁ = √n·M3/M2^1.5,
 * γ₂ = n·M4/M2² - 3). Excess kurtosis is 0 for Gaussian, positive
 * for heavy-tailed/peaked distributions.
 */
static void print_verdict(const struct iomoments_verdict *v)
{
	printf("\n  --- D007 verdict: %s ---\n",
	       iomoments_verdict_status_name(v->overall));
	for (size_t i = 0; i < v->n_signals; i++) {
		const struct iomoments_verdict_signal *s = &v->signals[i];
		printf("    %-22s %-7s  %s\n", s->name,
		       iomoments_verdict_status_name(s->status), s->rationale);
	}
	if (v->overall == IOMOMENTS_VERDICT_RED) {
		printf("\n  Moment-based summary refused. Recommended"
		       " alternatives:\n"
		       "    DDSketch — quantile estimation with relative"
		       " error guarantee\n"
		       "    HDR Histogram — bounded-error full latency"
		       " distribution\n");
	}
}

static void print_spectral(const struct iomoments_spectral_result *spec)
{
	printf("\n  --- Spectral-flatness sweep (D013) ---\n");
	if (spec->insufficient_data) {
		printf("  insufficient windows for spectral sweep\n");
		return;
	}
	printf("  k    W' (s)      n_virt  var_obs       var_clt       "
	       "ratio\n");
	for (size_t i = 0; i < spec->n_points; i++) {
		const struct iomoments_spectral_point *p = &spec->points[i];
		printf("  %-4zu %-10.4f  %-6zu  %-12.3f  %-12.3f  %.3f\n", p->k,
		       p->window_seconds, p->n_virtual_windows, p->var_observed,
		       p->var_predicted_clt, p->ratio);
	}
	if (spec->min_ratio_idx < spec->n_points) {
		printf("  → min ratio %.3f at W' = %.4f s\n", spec->min_ratio,
		       spec->points[spec->min_ratio_idx].window_seconds);
	}
}

static void print_level2(const struct iomoments_level2_result *l2)
{
	printf("\n  --- Level 2 (D013): Nyquist + stationarity diagnostics"
	       " ---\n");
	if (l2->insufficient_data) {
		printf("  insufficient windows for Level 2 (n=%zu < 4)\n",
		       l2->n_windows);
		return;
	}
	printf("  Var(windowed mean)     : %.3f ns²\n",
	       l2->var_of_windowed_mean);
	printf("  CLT-predicted variance : %.3f ns²"
	       "  (σ²/n_per_window)\n",
	       l2->clt_predicted_var);
	printf("  variance ratio (V/V₀)  : %.3f"
	       "  (1.0 = stationary Nyquist-met)\n",
	       l2->variance_ratio);
	printf("  Nyquist confidence     : %.3f  ∈ [0,1]\n",
	       l2->nyquist_confidence);
	printf("  autocorr(m1) at lags   : ");
	for (size_t li = 0; li < IOMOMENTS_LEVEL2_LAGS; li++) {
		printf("k=%zu: %+.3f%s", iomoments_level2_lag_values[li],
		       l2->autocorr[li],
		       (li + 1 < IOMOMENTS_LEVEL2_LAGS) ? "  " : "\n");
	}
}

static void print_report(const struct iomoments_summary *global, int duration,
			 int window_ms, size_t windows_captured,
			 const struct iomoments_level2_result *l2,
			 const struct iomoments_spectral_result *spec,
			 const struct iomoments_verdict *verdict)
{
	printf("\niomoments report (duration %d s, window %d ms)\n", duration,
	       window_ms);
	printf("====================================================\n");
	if (global->n == 0) {
		printf("  samples: 0  (no block I/O observed; run against"
		       " a workload generator like `fio`)\n");
		return;
	}
	double mean = iomoments_summary_mean(global);
	double variance = iomoments_summary_variance(global);
	double stddev = sqrt(variance);
	double skew = iomoments_summary_skewness(global);
	double kurt = iomoments_summary_excess_kurtosis(global);
	double samples_per_window =
		windows_captured > 0
			? (double)global->n / (double)windows_captured
			: 0.0;
	printf("  windows captured: %zu  (avg %.1f samples/window)\n",
	       windows_captured, samples_per_window);
	printf("  samples         : %lu\n", global->n);
	printf("  mean latency    : %.3f ns (%.3f μs)\n", mean, mean / 1e3);
	printf("  variance        : %.3f ns²\n", variance);
	printf("  stddev          : %.3f ns (%.3f μs)\n", stddev, stddev / 1e3);
	printf("  skewness        : %+.4f\n", skew);
	printf("  excess kurtosis : %+.4f\n", kurt);
	print_level2(l2);
	print_spectral(spec);
	print_verdict(verdict);
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"Usage: %s [--duration=<secs>] [--window=<ms>] [--help]\n"
		"\n"
		"  --duration=<secs>  Observation window in seconds"
		" (default %d).\n"
		"  --window=<ms>      Per-CPU drain cadence in milliseconds"
		" (default %d).\n"
		"                     Each window becomes one sample in"
		" D013's Level 2\n"
		"                     time-series. Smaller windows give finer"
		" Nyquist\n"
		"                     resolution but more drain overhead.\n"
		"  --help             Show this help.\n"
		"\n"
		"Requires CAP_BPF + CAP_PERFMON (or root) to load the"
		" BPF program.\n",
		argv0, IOMOMENTS_DEFAULT_DURATION, IOMOMENTS_DEFAULT_WINDOW_MS);
}

static int parse_args(int argc, char **argv, int *duration, int *window_ms)
{
	*duration = IOMOMENTS_DEFAULT_DURATION;
	*window_ms = IOMOMENTS_DEFAULT_WINDOW_MS;
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--help") == 0 ||
		    strcmp(argv[i], "-h") == 0) {
			usage(argv[0]);
			return 1;
		}
		if (strncmp(argv[i], "--duration=", 11) == 0) {
			char *end;
			long v = strtol(argv[i] + 11, &end, 10);
			if (*end != '\0' || v <= 0 || v > 3600) {
				fprintf(stderr, "iomoments: --duration must be"
						" 1..3600 seconds.\n");
				return -1;
			}
			*duration = (int)v;
			continue;
		}
		if (strncmp(argv[i], "--window=", 9) == 0) {
			char *end;
			long v = strtol(argv[i] + 9, &end, 10);
			if (*end != '\0' || v <= 0 || v > 60000) {
				fprintf(stderr, "iomoments: --window must be"
						" 1..60000 milliseconds.\n");
				return -1;
			}
			*window_ms = (int)v;
			continue;
		}
		fprintf(stderr, "iomoments: unknown arg %s\n", argv[i]);
		usage(argv[0]);
		return -1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	int duration;
	int window_ms;
	int rc = parse_args(argc, argv, &duration, &window_ms);
	if (rc > 0) {
		return 0;
	}
	if (rc < 0) {
		return 2;
	}

	libbpf_set_print(libbpf_print_quiet);
	signal(SIGINT, stop_handler);
	signal(SIGTERM, stop_handler);

	struct bpf_object *obj =
		bpf_object__open_file(IOMOMENTS_BPF_OBJECT, NULL);
	if (!obj) {
		fprintf(stderr, "iomoments: bpf_object__open_file(%s): %s\n",
			IOMOMENTS_BPF_OBJECT, strerror(errno));
		return 3;
	}
	if (bpf_object__load(obj) != 0) {
		fprintf(stderr, "iomoments: bpf_object__load failed — need"
				" CAP_BPF / CAP_PERFMON or root?\n");
		bpf_object__close(obj);
		return 4;
	}

	/*
	 * Attach every program in the ELF. iomoments.bpf.c declares two
	 * (iomoments_rq_issue, iomoments_rq_complete); both must
	 * succeed for the report to be meaningful.
	 */
	struct bpf_program *prog;
	bpf_object__for_each_program(prog, obj)
	{
		const struct bpf_link *link = bpf_program__attach(prog);
		if (!link) {
			fprintf(stderr, "iomoments: attach %s failed: %s\n",
				bpf_program__name(prog), strerror(errno));
			bpf_object__close(obj);
			return 5;
		}
		/* link is leaked on purpose: libbpf cleans up on
		 * bpf_object__close at exit. */
	}

	struct bpf_map *map =
		bpf_object__find_map_by_name(obj, IOMOMENTS_MAP_NAME);
	if (!map) {
		fprintf(stderr, "iomoments: map %s not found in object.\n",
			IOMOMENTS_MAP_NAME);
		bpf_object__close(obj);
		return 6;
	}
	int map_fd = bpf_map__fd(map);

	int ncpu = libbpf_num_possible_cpus();
	if (ncpu <= 0) {
		fprintf(stderr, "iomoments: libbpf_num_possible_cpus: %d\n",
			ncpu);
		bpf_object__close(obj);
		return 7;
	}

	/*
	 * Allocate the windowed-summary ring up-front. Capacity is
	 * (duration_s · 1000 / window_ms) + a small margin for the
	 * end-of-duration final drain. Bounded — no realloc.
	 */
	size_t ring_capacity = (size_t)duration * 1000 / (size_t)window_ms + 4;
	struct iomoments_window *window_ring =
		calloc(ring_capacity, sizeof(*window_ring));
	if (!window_ring) {
		perror("calloc window_ring");
		bpf_object__close(obj);
		return 7;
	}

	fprintf(stderr,
		"iomoments: attached; sampling for %d s, %d ms drain"
		" cadence (~%zu windows)...\n",
		duration, window_ms, ring_capacity - 4);

	size_t windows_count = run_drain_loop(map_fd, ncpu, duration, window_ms,
					      window_ring, ring_capacity);
	if (windows_count == SIZE_MAX) {
		free(window_ring);
		bpf_object__close(obj);
		return 8;
	}

	/*
	 * Aggregate the ring into a single global summary via pebay.h's
	 * parallel-combine rule — same shape report as before. Level 2
	 * analysis (D013) reads window_ring directly in a follow-up.
	 */
	struct iomoments_summary global = IOMOMENTS_SUMMARY_ZERO;
	for (size_t i = 0; i < windows_count; i++) {
		iomoments_summary_merge(&global, &window_ring[i].summary);
	}
	struct iomoments_level2_result l2;
	iomoments_level2_analyze(window_ring, windows_count, &global, &l2);
	struct iomoments_spectral_result spec;
	double base_window_seconds = (double)window_ms / 1000.0;
	iomoments_spectral_sweep(window_ring, windows_count, &global,
				 base_window_seconds, &spec);
	struct iomoments_verdict verdict;
	iomoments_verdict_compute(&global, window_ring, windows_count, &l2,
				  &spec, &verdict);
	print_report(&global, duration, window_ms, windows_count, &l2, &spec,
		     &verdict);

	free(window_ring);
	bpf_object__close(obj);
	return 0;
}
