/* SPDX-License-Identifier: GPL-2.0-only OR AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * iomoments BPF program.
 *
 * Attaches to the raw syscall-enter tracepoint and accumulates a
 * per-CPU running summary of the ktime_ns sample using pebay_bpf.h
 * (D011 fixed-point Welford). Userspace reads the per-CPU maps,
 * merges them via pebay.h (D006 canonical math), and reports
 * moments + verdicts per D007.
 *
 * Current scope: sys_enter attach as a stand-in for the final
 * block-layer instrumentation (D007 specifies block_rq_issue →
 * block_rq_complete latency pairing). sys_enter exercises the BPF
 * map + helper + fixed-point update chain against real sample
 * values — the infrastructure piece. Block-layer pairing is its
 * own follow-up because the ABI for block tracepoint args varies
 * between kernel versions (CO-RE relocations required) and the
 * issue→complete timestamp pairing needs a HASH map keyed by
 * request pointer. Those are additive; this foundation stands.
 *
 * **Dual-licensed** (GPL-2.0-only OR AGPL-3.0-or-later). D001
 * rationale: the kernel's license_is_gpl_compatible() allowlist
 * in kernel/bpf/core.c does not recognize the literal string
 * "AGPL", even though AGPL-3.0 is GPL-compatible via AGPL §13.
 * The GPL-2.0-only branch grants BPF programs access to GPL-only
 * tracing helpers (bpf_probe_read_kernel, bpf_get_stackid,
 * etc.); the AGPL-3.0-or-later branch keeps the project's
 * license identity consistent.
 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

#include "pebay_bpf.h"

/*
 * The kernel's BPF loader identifies the program's license by
 * reading the contents of the ELF `license` section (placed here
 * via the SEC macro), not by the C variable's name. The name
 * `_license` is a libbpf-ecosystem convention; libbpf's generated
 * skeletons assume it.
 */
/* NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp) */
char _license[] SEC("license") = "GPL";

/*
 * Per-CPU running summary. Each CPU maintains its own
 * iomoments_summary_bpf, accumulated lock-free. Userspace reads
 * all CPUs' entries (BPF maps expose the per-CPU array as one
 * value per CPU) and merges them via pebay.h's Pébay
 * parallel-combine rule.
 *
 * Single-key map (key=0): iomoments currently tracks one global
 * summary. Future extensions may key by (dev_major, dev_minor)
 * or similar to segment by I/O device; that's orthogonal to the
 * fixed-point math.
 */
/*
 * Map declaration uses __uint(key_size, ...) + __uint(value_size, ...)
 * rather than the newer __type(key, ...) / __type(value, ...) macros
 * because __type expands to `typeof(val) *name` and -Wpedantic flags
 * typeof as a GNU extension. Functionally equivalent; libbpf
 * recognizes both forms.
 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__uint(key_size, sizeof(__u32));
	__uint(value_size, sizeof(struct iomoments_summary_bpf));
} iomoments_summary SEC(".maps");

SEC("raw_tracepoint/sys_enter")
int iomoments_record_event(void *ctx)
{
	(void)ctx;
	__u32 key = 0;
	struct iomoments_summary_bpf *s =
		bpf_map_lookup_elem(&iomoments_summary, &key);
	if (!s) {
		return 0;
	}
	/*
	 * sys_enter fires at every syscall boundary. As a stand-in
	 * sample we take the current nanosecond timestamp modulo a
	 * bounded window so x stays under the Q32.32 limit in
	 * pebay_bpf.h. Real I/O-latency instrumentation computes
	 * (complete_ns - issue_ns) and feeds that in; the math is
	 * identical.
	 */
	__u64 t_ns = bpf_ktime_get_ns();
	iomoments_summary_bpf_update(s, t_ns & 0x3FFFFFFFULL);
	return 0;
}
