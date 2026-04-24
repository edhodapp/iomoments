/* SPDX-License-Identifier: GPL-2.0-only OR AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * iomoments BPF program — skeleton.
 *
 * This is the first load-bearing BPF object in iomoments. Today
 * it attaches to the sys_enter raw tracepoint and returns
 * immediately; its purpose is to exercise the full BPF toolchain
 * end-to-end:
 *
 *   clang -target bpf -> build/iomoments.bpf.o
 *   vmtest (D012) boots a guest kernel from $(KERNEL_IMAGE)
 *   bpftool prog load iomoments.bpf.o inside the guest
 *   the guest verifier accepts or rejects
 *
 * Real Pébay math (src/pebay_bpf.h, pending per D011) inlines into
 * a richer attach point — block_rq_issue or similar per D007 — in a
 * follow-up commit once the toolchain is gate-proven. Per-CPU maps,
 * perf buffers, CO-RE relocations all belong to that commit, not
 * this one.
 *
 * **Dual-licensed** (GPL-2.0-only OR AGPL-3.0-or-later). D001
 * rationale: the kernel's license_is_gpl_compatible() allowlist in
 * kernel/bpf/core.c doesn't recognize the literal string "AGPL",
 * even though AGPL-3.0 is GPL-compatible via AGPL §13. The
 * GPL-2.0-only branch is what grants BPF programs access to
 * GPL-only tracing helpers (bpf_probe_read_kernel, bpf_get_stackid,
 * etc.); the AGPL-3.0-or-later branch keeps the project's license
 * identity consistent.
 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

#include "pebay_bpf.h"

/*
 * The kernel's BPF loader identifies the program's license by reading
 * the contents of the ELF `license` section (placed here via the SEC
 * macro), not by the C variable's name. The name `_license` is a
 * libbpf-ecosystem convention — every BPF program in the tree uses
 * it, and libbpf's own generated skeletons assume it — so we keep it
 * for consistency rather than because the C identifier itself has a
 * kernel-ABI meaning. Reserved-identifier warning fires on the
 * leading-underscore form regardless; suppressed inline.
 */
/* NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp) */
char _license[] SEC("license") = "GPL";

/*
 * Dummy per-CPU-ish summary so pebay_bpf.h's inline functions end up
 * in the BPF ELF and get verified by the kernel. The real attach
 * point + map-backed summary lands in the next commit; this just
 * proves the fixed-point header compiles and loads under -target bpf.
 */
/* NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp) */
static struct iomoments_summary_bpf _iomoments_compile_check =
	IOMOMENTS_SUMMARY_BPF_ZERO;

/*
 * The BPF loader finds this function by its ELF section
 * ("raw_tracepoint/sys_enter"), not by a C-level caller. cppcheck's
 * static view sees no C reference and its whole-program
 * unusedFunction check fires on every BPF handler; suppression is
 * file-scoped in tooling/cppcheck.suppress rather than inline
 * because inline suppresses don't stick for whole-program checks.
 */
SEC("raw_tracepoint/sys_enter")
int iomoments_noop(void *ctx)
{
	(void)ctx;
	/* Exercise pebay_bpf.h so the fixed-point update function is
	 * emitted into the BPF object and faces the verifier. Sample
	 * value 1 is arbitrary; we're proving the toolchain + verifier
	 * accept the math, not producing useful measurements yet. */
	iomoments_summary_bpf_update(&_iomoments_compile_check, 1);
	return 0;
}
