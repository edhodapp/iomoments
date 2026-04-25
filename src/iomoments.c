/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * iomoments userspace loader + aggregator.
 *
 * Runs the iomoments BPF program attached to blk_mq_start_request /
 * blk_mq_end_request via fentry, collects the per-CPU fixed-point
 * running summaries, merges them with pebay.h's double-precision
 * Pébay parallel-combine rule, and prints a shape report.
 *
 * Usage:
 *
 *     iomoments [--duration=<secs>] [--help]
 *
 * Today's scope (beta trial foundation):
 *
 *   - Loads build/iomoments.bpf.o via libbpf.
 *   - Attaches both BPF programs (blk_mq_start_request fentry,
 *     blk_mq_end_request fentry).
 *   - Sleeps for --duration seconds (default 10).
 *   - Reads the iomoments_summary per-CPU map.
 *   - Merges per-CPU summaries into a single global summary via
 *     pebay.h's k=4 parallel-combine rule.
 *   - Prints n, mean latency (ns), variance (ns²), stddev (ns),
 *     skewness, excess kurtosis.
 *
 * NOT yet in scope (follow-up):
 *
 *   - Diagnostic battery + Green/Yellow/Amber/Red verdict (D007).
 *   - Repeated sampling / reporting windows.
 *   - Device / workload-class segmentation.
 */

#include <errno.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "pebay.h"
#include "pebay_bpf.h"

#define IOMOMENTS_BPF_OBJECT "build/iomoments.bpf.o"
#define IOMOMENTS_MAP_NAME "iomoments_summary"
#define IOMOMENTS_DEFAULT_DURATION 10

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
 * Walk the per-CPU map, merge each CPU's summary into `global` using
 * pebay.h's parallel-combine rule.
 */
static int merge_percpu_summaries(int map_fd, int ncpu,
				  struct iomoments_summary *global)
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

	for (int cpu = 0; cpu < ncpu; cpu++) {
		struct iomoments_summary cpu_ref = IOMOMENTS_SUMMARY_ZERO;
		bpf_summary_to_ref(&percpu_values[cpu], &cpu_ref);
		iomoments_summary_merge(global, &cpu_ref);
	}

	free(percpu_values);
	return 0;
}

/*
 * Print the user-facing report. Units: ns everywhere. Variance is
 * the population variance (σ², m2/n), per D006. Skewness and excess
 * kurtosis are dimensionless population moments (γ₁ = √n·M3/M2^1.5,
 * γ₂ = n·M4/M2² - 3). Excess kurtosis is 0 for Gaussian, positive
 * for heavy-tailed/peaked distributions.
 */
static void print_report(const struct iomoments_summary *global, int duration)
{
	printf("\niomoments report (duration %d s, D007 verdict layer"
	       " pending)\n",
	       duration);
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
	printf("  samples         : %lu\n", global->n);
	printf("  mean latency    : %.3f ns (%.3f μs)\n", mean, mean / 1e3);
	printf("  variance        : %.3f ns²\n", variance);
	printf("  stddev          : %.3f ns (%.3f μs)\n", stddev, stddev / 1e3);
	printf("  skewness        : %+.4f\n", skew);
	printf("  excess kurtosis : %+.4f\n", kurt);
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"Usage: %s [--duration=<secs>] [--help]\n"
		"\n"
		"  --duration=<secs>  Observation window in seconds"
		" (default %d).\n"
		"  --help             Show this help.\n"
		"\n"
		"Requires CAP_BPF + CAP_PERFMON (or root) to load the"
		" BPF program.\n",
		argv0, IOMOMENTS_DEFAULT_DURATION);
}

static int parse_args(int argc, char **argv, int *duration)
{
	*duration = IOMOMENTS_DEFAULT_DURATION;
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
		fprintf(stderr, "iomoments: unknown arg %s\n", argv[i]);
		usage(argv[0]);
		return -1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	int duration;
	int rc = parse_args(argc, argv, &duration);
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

	fprintf(stderr, "iomoments: attached; sampling for %d s...\n",
		duration);
	for (int remaining = duration; remaining > 0 && !stop_flag;
	     remaining--) {
		sleep(1);
	}

	struct iomoments_summary global = IOMOMENTS_SUMMARY_ZERO;
	rc = merge_percpu_summaries(map_fd, ncpu, &global);
	if (rc) {
		bpf_object__close(obj);
		return 8;
	}
	print_report(&global, duration);
	bpf_object__close(obj);
	return 0;
}
