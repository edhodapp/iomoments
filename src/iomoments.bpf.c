/* SPDX-License-Identifier: GPL-2.0-only OR AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * iomoments BPF program — block I/O latency shape characterization.
 *
 * Attaches to the BTF-typed block_rq_issue and block_rq_complete
 * tracepoints. On issue, records bpf_ktime_get_ns() keyed by
 * struct-request pointer in a HASH map. On complete, looks up the
 * start timestamp, computes latency_ns = end_ts - start_ts, and feeds
 * it into a per-CPU running summary via pebay_bpf.h (D011).
 *
 * Userspace reads the per-CPU summary map, merges entries with
 * pebay.h (D006 canonical math), runs the diagnostic battery (D007),
 * and reports moments + verdict.
 *
 * Design choices (per D007, D011, D012):
 *
 *   tp_btf vs fentry: Earlier revisions used fentry on
 *   blk_mq_start_request / blk_mq_end_request. That worked on
 *   single-request completion paths but **silently bypassed
 *   blk_mq_end_request_batch** (the batched-completion path used by
 *   NVMe and other modern drivers). On a 2026-04-26 measurement
 *   against an NVMe-backed host: 20,015 issues observed, 2 completes
 *   observed — a ~99.99% miss rate. iomoments would have undercounted
 *   completions catastrophically on every NVMe deployment, breaking
 *   the n counter and every downstream diagnostic. The fix: attach
 *   to the block_rq_issue / block_rq_complete tracepoints instead.
 *   Both fire per-request from *every* completion path, including
 *   inside blk_mq_end_request_batch's loop. tp_btf gives us the
 *   typed `struct request *rq` argument directly (no composite-key
 *   workaround that the classic tracepoint format would force).
 *
 *   struct request is declared opaque (forward decl only). No
 *   vmlinux.h needed because we never dereference — only use the
 *   pointer value as a HASH map key.
 *
 *   Latency saturation at 2^31 ns (~2.1 s): Q32.32 m1 in
 *   pebay_bpf.h overflows on single samples above that. Capped
 *   here; catastrophic multi-second stalls become saturated
 *   samples until the diagnostic layer (D007 future signal)
 *   surfaces them as a separate channel.
 *
 * **Dual-licensed** (GPL-2.0-only OR AGPL-3.0-or-later). D001
 * rationale: the kernel's license_is_gpl_compatible() allowlist in
 * kernel/bpf/core.c does not recognize the literal string "AGPL",
 * even though AGPL-3.0 is GPL-compatible via AGPL §13. The
 * GPL-2.0-only branch grants BPF programs access to GPL-only
 * tracing helpers (bpf_probe_read_kernel, bpf_get_stackid, etc.);
 * the AGPL-3.0-or-later branch keeps the project's license
 * identity consistent.
 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "iomoments_topk.h"
#include "pebay_bpf.h"

/*
 * Opaque forward declaration. iomoments never dereferences; we use
 * the pointer value as a HASH map key. This avoids pulling vmlinux.h
 * for a type whose layout we don't care about.
 */
struct request;

/*
 * `_license` is a libbpf convention: kernel reads the "license"
 * ELF section to check license_is_gpl_compatible() before granting
 * GPL-only helper access. Can't rename.
 */
/* NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp) */
char _license[] SEC("license") = "GPL";

/*
 * issue-timestamp map: key = (struct request *), value = ns timestamp.
 * Bounded at 10240 in-flight requests per iomoments instance —
 * well above typical in-flight depth; BPF HASH eviction handles
 * overflow by returning NULL on lookup (iomoments just drops the
 * sample rather than producing wrong latency).
 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 10240);
	__uint(key_size, sizeof(__u64));
	__uint(value_size, sizeof(__u64));
} iomoments_req_start SEC(".maps");

/*
 * Per-CPU running summary. Single-key today; future extensions may
 * key by device or request class.
 * __uint(key_size/value_size, ...) rather than __type(...) because
 * __type expands to `typeof(val) *name` and -Wpedantic flags typeof.
 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__uint(key_size, sizeof(__u32));
	__uint(value_size, sizeof(struct iomoments_summary_bpf));
} iomoments_summary SEC(".maps");

/*
 * Per-CPU top-K reservoir for the Hill (1975) tail-index estimator.
 * Parallel to iomoments_summary; same drain cadence, separate
 * value type. K=32 entries × 8 bytes + 8 bytes overhead = ~264
 * bytes per CPU.
 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__uint(key_size, sizeof(__u32));
	__uint(value_size, sizeof(struct iomoments_topk));
} iomoments_topk_map SEC(".maps");

SEC("tp_btf/block_rq_issue")
int BPF_PROG(iomoments_rq_issue, struct request *rq)
{
	__u64 rq_key = (__u64)(unsigned long)rq;
	__u64 ts = bpf_ktime_get_ns();
	bpf_map_update_elem(&iomoments_req_start, &rq_key, &ts, BPF_ANY);
	return 0;
}

/*
 * block_rq_complete tracepoint signature is
 * (struct request *rq, blk_status_t error, unsigned int nr_bytes).
 * blk_status_t is a kernel typedef of u8; we use `unsigned int` to
 * avoid pulling in vmlinux.h for a type whose value we ignore. BTF
 * widens the u8 to a register-sized scalar at the trampoline so the
 * declared `unsigned int` matches what the verifier sees.
 *
 * Timing baseline note for future maintainers: block_rq_issue
 * fires from inside blk_mq_start_request via trace_block_rq_issue(rq);
 * the timestamp shift vs the prior fentry attach is a handful of ns,
 * negligible for microsecond-scale I/O latency.
 */
SEC("tp_btf/block_rq_complete")
int BPF_PROG(iomoments_rq_complete, struct request *rq, unsigned int error,
	     unsigned int nr_bytes)
{
	(void)error;
	(void)nr_bytes;
	__u64 rq_key = (__u64)(unsigned long)rq;
	const __u64 *start_ts_p =
		bpf_map_lookup_elem(&iomoments_req_start, &rq_key);
	if (!start_ts_p) {
		return 0;
	}
	__u64 end_ts = bpf_ktime_get_ns();
	__u64 start_ts = *start_ts_p;
	bpf_map_delete_elem(&iomoments_req_start, &rq_key);

	if (end_ts <= start_ts) {
		/* Clock glitch or the request's start_ts was overwritten;
		 * drop rather than feed a negative/zero sample. */
		return 0;
	}
	__u64 latency_ns = end_ts - start_ts;
	/*
	 * Saturate at Q32.32 limit so pebay_bpf.h's x << 32 stays in
	 * int64. Samples above ~2.1 s become saturated values;
	 * diagnostic layer (future D007 signal) catches them via a
	 * separate outlier channel.
	 */
	if (latency_ns >= (1ULL << 31)) {
		latency_ns = (1ULL << 31) - 1;
	}

	__u32 key = 0;
	struct iomoments_summary_bpf *s =
		bpf_map_lookup_elem(&iomoments_summary, &key);
	if (!s) {
		return 0;
	}
	iomoments_summary_bpf_update(s, latency_ns);

	/* Feed the same sample into the top-K reservoir. The Hill
	 * tail-index signal reads this at verdict-compute time. */
	struct iomoments_topk *t =
		bpf_map_lookup_elem(&iomoments_topk_map, &key);
	if (t) {
		iomoments_topk_insert(t, latency_ns);
	}
	return 0;
}
