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
#include <limits.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "iomoments_level2.h"
#include "iomoments_spectral.h"
#include "iomoments_topk.h"
#include "iomoments_verdict.h"
#include "pebay.h"
#include "pebay_bpf.h"

#define IOMOMENTS_BPF_OBJECT "iomoments.bpf.o"
#define IOMOMENTS_CONFIG_MAP_NAME "iomoments_config"
#define IOMOMENTS_RAW_SAMPLES_MAP_NAME "iomoments_raw_samples"
/*
 * k=3 fallback per D014 / #48 — drops the m4 update body so the
 * program fits stricter verifier budgets (6.17+). Tried only when
 * the default k=4 object fails to load. The verdict layer reads
 * `loaded_order` to YELLOW the m4-dependent signals.
 */
#define IOMOMENTS_BPF_OBJECT_K3 "iomoments-k3.bpf.o"
#define IOMOMENTS_MAP_NAME "iomoments_summary"
#define IOMOMENTS_TOPK_MAP_NAME "iomoments_topk_map"
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
 * Raw-sample dump state, opaque to run_drain_loop. raw_file is the
 * append-only binary file receiving __u64 latency samples;
 * raw_samples_written counts successful fwrites for the end-of-run
 * report; raw_samples_lost increments on fwrite short-write (rare,
 * disk-full / SIGPIPE). The ringbuf-side overflow is tracked by
 * libbpf internally and surfaced via ring_buffer__free's epilogue.
 */
struct iomoments_raw_dump {
	FILE *file;
	uint64_t samples_written;
	uint64_t samples_lost;
};

/*
 * libbpf ring-buffer callback: each invocation receives one raw
 * latency sample (8 bytes, __u64 ns). Append to the dump file.
 * Return 0 unconditionally so the ringbuf consumer doesn't bail on
 * a transient short-write — `samples_lost` records the loss for
 * the end-of-run report.
 *
 * `data` is intentionally non-const: libbpf's
 * ring_buffer_sample_fn typedef is
 * `int (*)(void *ctx, void *data, size_t)`. Const here would
 * require a function-pointer cast at ring_buffer__new and lose
 * type checking for no real benefit.
 */
/* cppcheck-suppress constParameterCallback */
static int handle_raw_sample(void *ctx, void *data, size_t data_sz)
{
	struct iomoments_raw_dump *d = ctx;
	if (data_sz != sizeof(uint64_t)) {
		d->samples_lost += 1;
		return 0;
	}
	if (fwrite(data, sizeof(uint64_t), 1, d->file) == 1) {
		d->samples_written += 1;
	} else {
		d->samples_lost += 1;
	}
	return 0;
}

/*
 * Prepare the raw-sample dump path: open the file, flip the BPF
 * config map to enable dumping, allocate the libbpf ring buffer
 * consumer. Returns the consumer (caller frees with
 * ring_buffer__free) or NULL on failure (with diagnostic on stderr;
 * the file is closed before return so the caller doesn't have to).
 */
static struct ring_buffer *raw_dump_setup(struct bpf_object *obj,
					  const char *path,
					  struct iomoments_raw_dump *out)
{
	out->file = fopen(path, "wb");
	if (!out->file) {
		fprintf(stderr, "iomoments: open %s for raw dump: %s\n", path,
			strerror(errno));
		return NULL;
	}
	struct bpf_map *cfg_map =
		bpf_object__find_map_by_name(obj, IOMOMENTS_CONFIG_MAP_NAME);
	struct bpf_map *rb_map = bpf_object__find_map_by_name(
		obj, IOMOMENTS_RAW_SAMPLES_MAP_NAME);
	if (!cfg_map || !rb_map) {
		fprintf(stderr, "iomoments: raw-dump maps missing in BPF "
				"object;"
				" rebuild iomoments.bpf.o.\n");
		fclose(out->file);
		out->file = NULL;
		return NULL;
	}
	uint32_t cfg_key = 0;
	uint32_t cfg_val = 1;
	if (bpf_map_update_elem(bpf_map__fd(cfg_map), &cfg_key, &cfg_val,
				BPF_ANY) != 0) {
		fprintf(stderr, "iomoments: enable raw-dump config: %s\n",
			strerror(errno));
		fclose(out->file);
		out->file = NULL;
		return NULL;
	}
	struct ring_buffer *rb = ring_buffer__new(bpf_map__fd(rb_map),
						  handle_raw_sample, out, NULL);
	if (!rb) {
		fprintf(stderr,
			"iomoments: ring_buffer__new for raw samples: %s\n",
			strerror(errno));
		fclose(out->file);
		out->file = NULL;
		return NULL;
	}
	return rb;
}

static void raw_dump_teardown(struct ring_buffer *rb,
			      struct iomoments_raw_dump *d)
{
	if (rb) {
		ring_buffer__free(rb);
	}
	if (d->file) {
		fclose(d->file);
		d->file = NULL;
	}
	fprintf(stderr, "iomoments: raw-sample dump: %lu written, %lu lost.\n",
		(unsigned long)d->samples_written,
		(unsigned long)d->samples_lost);
}

/*
 * Resolve a BPF object basename ("iomoments.bpf.o") to a real path
 * relative to where this binary actually lives — not relative to the
 * caller's CWD. Tried in order:
 *
 *   1. Sibling of the running binary. Covers the dev workflow where
 *      the binary and BPF objects share `build/`.
 *   2. `${exe_dir}/../lib/iomoments/<basename>`. Covers `make install`
 *      with the standard FHS layout (binary in `${PREFIX}/bin`, BPF
 *      objects in `${PREFIX}/lib/iomoments/`).
 *   3. The basename CWD-relative — last-ditch dev fallback.
 *
 * Writes the resolved path into `out` (size `out_sz`) and returns 0
 * on success; -1 if no candidate exists.
 */
static int iomoments_resolve_bpf_object(const char *basename, char *out,
					size_t out_sz)
{
	char exe[PATH_MAX];
	ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
	struct stat st;
	if (n > 0) {
		exe[n] = '\0';
		char *slash = strrchr(exe, '/');
		if (slash != NULL) {
			*slash = '\0';
			int r = snprintf(out, out_sz, "%s/%s", exe, basename);
			if (r > 0 && (size_t)r < out_sz &&
			    stat(out, &st) == 0) {
				return 0;
			}
			r = snprintf(out, out_sz, "%s/../lib/iomoments/%s", exe,
				     basename);
			if (r > 0 && (size_t)r < out_sz &&
			    stat(out, &st) == 0) {
				return 0;
			}
		}
	}
	int r = snprintf(out, out_sz, "%s", basename);
	if (r > 0 && (size_t)r < out_sz && stat(out, &st) == 0) {
		return 0;
	}
	return -1;
}

/*
 * Resolve `basename` into a real path, open the BPF object, and load
 * it. On any failure, prints a diagnostic to stderr and returns NULL;
 * caller frees on success via bpf_object__close. `*load_errno_out`
 * receives the load errno (0 on resolve/open failure) so callers can
 * distinguish verifier rejections (E2BIG / EINVAL) from access /
 * resource failures.
 */
static struct bpf_object *iomoments_open_load(const char *basename,
					      int *load_errno_out)
{
	char path[PATH_MAX];
	*load_errno_out = 0;
	if (iomoments_resolve_bpf_object(basename, path, sizeof(path)) != 0) {
		fprintf(stderr,
			"iomoments: cannot locate %s — looked beside the"
			" binary and in ../lib/iomoments/.\n",
			basename);
		return NULL;
	}
	struct bpf_object *obj = bpf_object__open_file(path, NULL);
	if (!obj) {
		fprintf(stderr, "iomoments: bpf_object__open_file(%s): %s\n",
			path, strerror(errno));
		return NULL;
	}
	int load_rc = bpf_object__load(obj);
	if (load_rc != 0) {
		*load_errno_out = -load_rc;
		bpf_object__close(obj);
		return NULL;
	}
	return obj;
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
 * Drain the per-CPU summary map into a single merged windowed
 * summary, then reset all per-CPU values to zero. See top-of-file
 * note about the lookup→reset race.
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
 * Drain the per-CPU top-K map into a single merged windowed top-K,
 * then reset all per-CPU reservoirs. Same lookup→reset race as the
 * summary drain; same acceptable-loss tradeoff.
 */
static int drain_and_reset_topk(int topk_map_fd, int ncpu,
				struct iomoments_topk *out)
{
	struct iomoments_topk *percpu_values =
		calloc((size_t)ncpu, sizeof(*percpu_values));
	if (!percpu_values) {
		perror("calloc percpu_topk");
		return -ENOMEM;
	}

	__u32 key = 0;
	int rc = bpf_map_lookup_elem(topk_map_fd, &key, percpu_values);
	if (rc) {
		fprintf(stderr, "bpf_map_lookup_elem (topk): %s\n",
			strerror(errno));
		free(percpu_values);
		return rc;
	}

	iomoments_topk_init(out);
	for (int cpu = 0; cpu < ncpu; cpu++) {
		iomoments_topk_merge(out, &percpu_values[cpu]);
	}

	memset(percpu_values, 0, (size_t)ncpu * sizeof(*percpu_values));
	rc = bpf_map_update_elem(topk_map_fd, &key, percpu_values, BPF_ANY);
	free(percpu_values);
	if (rc) {
		fprintf(stderr, "bpf_map_update_elem (topk reset): %s\n",
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
 * Drain summary + top-K maps into the next ring slot.
 */
static int drain_one_window(int map_fd, int topk_map_fd, int ncpu,
			    uint64_t end_ts_ns, struct iomoments_window *out)
{
	struct iomoments_summary win = IOMOMENTS_SUMMARY_ZERO;
	if (drain_and_reset_summaries(map_fd, ncpu, &win) != 0) {
		return -1;
	}
	struct iomoments_topk topk;
	iomoments_topk_init(&topk);
	if (drain_and_reset_topk(topk_map_fd, ncpu, &topk) != 0) {
		return -1;
	}
	out->end_ts_ns = end_ts_ns;
	out->summary = win;
	out->topk = topk;
	return 0;
}

/*
 * Periodic-drain main loop. Wakes every window_ms via absolute
 * clock_nanosleep, drains the per-CPU summary + top-K maps into
 * the next ring slot, stops at duration or signal. Returns the
 * count of windows captured (including the final post-loop drain)
 * or SIZE_MAX on hard error.
 */
static size_t run_drain_loop(int map_fd, int topk_map_fd, int ncpu,
			     int duration, int window_ms,
			     struct iomoments_window *ring,
			     size_t ring_capacity, struct ring_buffer *raw_rb)
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
		if (count < ring_capacity) {
			if (drain_one_window(map_fd, topk_map_fd, ncpu,
					     timespec_to_ns(&next_wakeup),
					     &ring[count]) != 0) {
				return SIZE_MAX;
			}
			count += 1;
		}
		/* Drain the raw-sample ringbuf if D019 capture is on.
		 * Non-blocking poll (timeout 0): pulls everything ready,
		 * returns immediately. Steady-state at 50K IOPS / 100 ms
		 * windows is ~5K samples per poll, well under capacity. */
		if (raw_rb) {
			ring_buffer__poll(raw_rb, 0);
		}
	}

	/* Final drain: capture samples since the last periodic drain. */
	if (count < ring_capacity) {
		struct timespec final_ts;
		clock_gettime(CLOCK_MONOTONIC, &final_ts);
		if (drain_one_window(map_fd, topk_map_fd, ncpu,
				     timespec_to_ns(&final_ts),
				     &ring[count]) == 0) {
			count += 1;
		}
	}
	if (raw_rb) {
		ring_buffer__poll(raw_rb, 100); /* drain residual */
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

static void print_report_text(const struct iomoments_summary *global,
			      int duration, int window_ms,
			      size_t windows_captured,
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

/*
 * Minimal JSON-string escaper. Writes a JSON-quoted string for `s`
 * (including the surrounding quotes) to stdout. Handles the
 * RFC 8259 control set ("\", "\"", BS/FF/LF/CR/TAB) plus other
 * <0x20 chars as \u00xx. Sufficient for the rationale strings the
 * D007 verdict layer emits (short ASCII), defensively safe if a
 * future signal rationale grows a quote or backslash.
 */
static void print_json_string(const char *s)
{
	putchar('"');
	for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
		switch (*p) {
		case '"':
			fputs("\\\"", stdout);
			break;
		case '\\':
			fputs("\\\\", stdout);
			break;
		case '\b':
			fputs("\\b", stdout);
			break;
		case '\f':
			fputs("\\f", stdout);
			break;
		case '\n':
			fputs("\\n", stdout);
			break;
		case '\r':
			fputs("\\r", stdout);
			break;
		case '\t':
			fputs("\\t", stdout);
			break;
		default:
			if (*p < 0x20) {
				printf("\\u%04x", *p);
			} else {
				putchar(*p);
			}
		}
	}
	putchar('"');
}

static void print_level2_json(const struct iomoments_level2_result *l2)
{
	printf(",\"level2\":{\"insufficient_data\":%s",
	       l2->insufficient_data ? "true" : "false");
	if (l2->insufficient_data) {
		printf(",\"n_windows\":%zu}", l2->n_windows);
		return;
	}
	printf(",\"n_windows\":%zu,"
	       "\"var_of_windowed_mean\":%.6e,"
	       "\"clt_predicted_var\":%.6e,"
	       "\"variance_ratio\":%.6f,"
	       "\"nyquist_confidence\":%.6f,"
	       "\"autocorr\":{",
	       l2->n_windows, l2->var_of_windowed_mean, l2->clt_predicted_var,
	       l2->variance_ratio, l2->nyquist_confidence);
	for (size_t li = 0; li < IOMOMENTS_LEVEL2_LAGS; li++) {
		printf("%s\"k%zu\":%.6f", li == 0 ? "" : ",",
		       iomoments_level2_lag_values[li], l2->autocorr[li]);
	}
	printf("}}");
}

static void print_spectral_json(const struct iomoments_spectral_result *spec)
{
	printf(",\"spectral\":{\"insufficient_data\":%s",
	       spec->insufficient_data ? "true" : "false");
	if (spec->insufficient_data) {
		printf("}");
		return;
	}
	printf(",\"min_ratio\":%.6f,\"min_ratio_idx\":%zu,\"points\":[",
	       spec->min_ratio, spec->min_ratio_idx);
	for (size_t i = 0; i < spec->n_points; i++) {
		const struct iomoments_spectral_point *p = &spec->points[i];
		printf("%s{\"k\":%zu,\"window_seconds\":%.6f,"
		       "\"n_virtual_windows\":%zu,"
		       "\"var_observed\":%.6e,\"var_predicted_clt\":%.6e,"
		       "\"ratio\":%.6f}",
		       i == 0 ? "" : ",", p->k, p->window_seconds,
		       p->n_virtual_windows, p->var_observed,
		       p->var_predicted_clt, p->ratio);
	}
	printf("]}");
}

static void print_verdict_json(const struct iomoments_verdict *v)
{
	printf(",\"verdict\":{\"overall\":");
	print_json_string(iomoments_verdict_status_name(v->overall));
	printf(",\"signals\":[");
	for (size_t i = 0; i < v->n_signals; i++) {
		const struct iomoments_verdict_signal *s = &v->signals[i];
		printf("%s{\"name\":", i == 0 ? "" : ",");
		print_json_string(s->name);
		printf(",\"status\":");
		print_json_string(iomoments_verdict_status_name(s->status));
		printf(",\"rationale\":");
		print_json_string(s->rationale);
		printf("}");
	}
	printf("]}");
}

/*
 * Machine-readable counterpart to print_report_text. Emits a single
 * JSON object with the same fields the text path prints. Used by the
 * D019 calibration harness and by anything downstream that wants
 * structured iomoments output (spreadsheet ingest, Python analysis).
 *
 * Schema is documented inline by the field names; consumers should
 * tolerate additional fields in future versions (additive only).
 */
static void print_report_json(const struct iomoments_summary *global,
			      int duration, int window_ms,
			      size_t windows_captured,
			      const struct iomoments_level2_result *l2,
			      const struct iomoments_spectral_result *spec,
			      const struct iomoments_verdict *verdict,
			      const char *loaded_order_name)
{
	printf("{\"duration_s\":%d,\"window_ms\":%d,\"loaded_order\":",
	       duration, window_ms);
	print_json_string(loaded_order_name);
	printf(",\"samples\":%lu,\"windows_captured\":%zu", global->n,
	       windows_captured);
	if (global->n == 0) {
		printf("}\n");
		return;
	}
	double mean = iomoments_summary_mean(global);
	double variance = iomoments_summary_variance(global);
	double stddev = sqrt(variance);
	double skew = iomoments_summary_skewness(global);
	double kurt = iomoments_summary_excess_kurtosis(global);
	printf(",\"moments\":{"
	       "\"mean_ns\":%.6e,\"variance_ns2\":%.6e,\"stddev_ns\":%.6e,"
	       "\"skewness\":%.6f,\"excess_kurtosis\":%.6f}",
	       mean, variance, stddev, skew, kurt);
	print_level2_json(l2);
	print_spectral_json(spec);
	print_verdict_json(verdict);
	printf("}\n");
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"Usage: %s [--duration=<secs>] [--window=<ms>] [--json]"
		" [--help]\n"
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
		"  --json             Emit a single-line JSON object on stdout"
		" instead of\n"
		"                     the human-readable report. Used by D019"
		" calibration\n"
		"                     and other downstream consumers.\n"
		"  --dump-raw-samples=PATH\n"
		"                     Append every block_rq_complete latency"
		" (ns, uint64\n"
		"                     little-endian) to PATH for offline"
		" calibration analysis\n"
		"                     (D019 ground truth). Disabled by "
		"default.\n"
		"  --help             Show this help.\n"
		"\n"
		"Requires CAP_BPF + CAP_PERFMON (or root) to load the"
		" BPF program.\n",
		argv0, IOMOMENTS_DEFAULT_DURATION, IOMOMENTS_DEFAULT_WINDOW_MS);
}

struct iomoments_cli_args {
	int duration;
	int window_ms;
	int json_mode;
	const char *dump_raw_path; /* NULL when --dump-raw-samples is unset */
};

static int parse_long_arg(const char *arg, const char *prefix, long min,
			  long max, const char *err_label, long *out)
{
	size_t plen = strlen(prefix);
	if (strncmp(arg, prefix, plen) != 0) {
		return 0;
	}
	char *end;
	long v = strtol(arg + plen, &end, 10);
	if (*end != '\0' || v < min || v > max) {
		fprintf(stderr, "iomoments: %s must be %ld..%ld.\n", err_label,
			min, max);
		return -1;
	}
	*out = v;
	return 1;
}

/*
 * Try a single argv entry against every recognized flag.
 * Returns 1 if matched, 0 if no match (caller errors with "unknown arg"),
 * -1 on parse error (caller exits non-zero).
 */
static int dispatch_arg(const char *arg, struct iomoments_cli_args *out)
{
	if (strcmp(arg, "--json") == 0) {
		out->json_mode = 1;
		return 1;
	}
	if (strncmp(arg, "--dump-raw-samples=", 19) == 0) {
		const char *path = arg + 19;
		if (path[0] == '\0') {
			fprintf(stderr, "iomoments: --dump-raw-samples= needs"
					" a path.\n");
			return -1;
		}
		out->dump_raw_path = path;
		return 1;
	}
	long v = 0;
	int rc = parse_long_arg(arg, "--duration=", 1, 3600,
				"--duration (seconds)", &v);
	if (rc < 0) {
		return -1;
	}
	if (rc > 0) {
		out->duration = (int)v;
		return 1;
	}
	rc = parse_long_arg(arg, "--window=", 1, 60000, "--window (ms)", &v);
	if (rc < 0) {
		return -1;
	}
	if (rc > 0) {
		out->window_ms = (int)v;
		return 1;
	}
	return 0;
}

static int parse_args(int argc, char **argv, struct iomoments_cli_args *out)
{
	out->duration = IOMOMENTS_DEFAULT_DURATION;
	out->window_ms = IOMOMENTS_DEFAULT_WINDOW_MS;
	out->json_mode = 0;
	out->dump_raw_path = NULL;
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--help") == 0 ||
		    strcmp(argv[i], "-h") == 0) {
			usage(argv[0]);
			return 1;
		}
		int rc = dispatch_arg(argv[i], out);
		if (rc < 0) {
			return -1;
		}
		if (rc > 0) {
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
	struct iomoments_cli_args args;
	int rc = parse_args(argc, argv, &args);
	if (rc > 0) {
		return 0;
	}
	if (rc < 0) {
		return 2;
	}
	int duration = args.duration;
	int window_ms = args.window_ms;

	libbpf_set_print(libbpf_print_quiet);
	signal(SIGINT, stop_handler);
	signal(SIGTERM, stop_handler);

	/* Try the k=4 default first; on verifier rejection (-E2BIG or
	 * EINVAL — both indicate the verifier didn't accept the
	 * program) fall back to k=3 per D014. Other failures (EPERM
	 * from missing CAP_BPF, ENOMEM, etc.) are NOT verifier
	 * rejections — surface them directly rather than silently
	 * retry against k=3 and produce a misleading aggregate error. */
	enum iomoments_moment_order loaded_order = IOMOMENTS_MOMENT_ORDER_K4;
	int k4_errno = 0;
	struct bpf_object *obj =
		iomoments_open_load(IOMOMENTS_BPF_OBJECT, &k4_errno);
	if (!obj) {
		if (k4_errno != E2BIG && k4_errno != EINVAL) {
			fprintf(stderr,
				"iomoments: bpf_object__load failed: %s"
				" (need CAP_BPF / CAP_PERFMON or root?)\n",
				strerror(k4_errno));
			return 4;
		}
		fprintf(stderr,
			"iomoments: k=4 variant rejected by verifier (%s);"
			" falling back to k=3 (m4-dependent signals will"
			" report YELLOW per D014).\n",
			strerror(k4_errno));
		int k3_errno = 0;
		obj = iomoments_open_load(IOMOMENTS_BPF_OBJECT_K3, &k3_errno);
		if (!obj) {
			fprintf(stderr,
				"iomoments: k=3 fallback also rejected (%s);"
				" verifier may need a future k=2 variant.\n",
				strerror(k3_errno));
			return 4;
		}
		loaded_order = IOMOMENTS_MOMENT_ORDER_K3;
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

	struct bpf_map *topk_map =
		bpf_object__find_map_by_name(obj, IOMOMENTS_TOPK_MAP_NAME);
	if (!topk_map) {
		fprintf(stderr, "iomoments: map %s not found in object.\n",
			IOMOMENTS_TOPK_MAP_NAME);
		bpf_object__close(obj);
		return 6;
	}
	int topk_map_fd = bpf_map__fd(topk_map);

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

	struct iomoments_raw_dump raw_dump = {
		.file = NULL,
		.samples_written = 0,
		.samples_lost = 0,
	};
	struct ring_buffer *raw_rb = NULL;
	if (args.dump_raw_path) {
		raw_rb = raw_dump_setup(obj, args.dump_raw_path, &raw_dump);
		if (!raw_rb) {
			free(window_ring);
			bpf_object__close(obj);
			return 9;
		}
	}

	fprintf(stderr,
		"iomoments: attached; sampling for %d s, %d ms drain"
		" cadence (~%zu windows)...\n",
		duration, window_ms, ring_capacity - 4);

	size_t windows_count = run_drain_loop(map_fd, topk_map_fd, ncpu,
					      duration, window_ms, window_ring,
					      ring_capacity, raw_rb);
	if (windows_count == SIZE_MAX) {
		raw_dump_teardown(raw_rb, &raw_dump);
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
				  &spec, loaded_order, &verdict);
	if (args.json_mode) {
		const char *order_name =
			loaded_order == IOMOMENTS_MOMENT_ORDER_K3 ? "k3" : "k4";
		print_report_json(&global, duration, window_ms, windows_count,
				  &l2, &spec, &verdict, order_name);
	} else {
		print_report_text(&global, duration, window_ms, windows_count,
				  &l2, &spec, &verdict);
	}

	raw_dump_teardown(raw_rb, &raw_dump);
	free(window_ring);
	bpf_object__close(obj);
	return 0;
}
