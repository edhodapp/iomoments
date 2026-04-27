#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ed Hodapp <ed@hodapp.com>
#
# Honest per-event overhead measurement for the iomoments BPF program.
#
# Uses the kernel's built-in BPF run-time accounting (introduced in
# 5.1 via sysctl kernel.bpf_stats_enabled). When enabled, the kernel
# tracks `run_time_ns` and `run_cnt` per BPF program — total CPU time
# spent inside the program and number of times it ran. `bpftool prog
# show -j` exposes those counters. Dividing gives a direct
# average-cost-per-event number with no sampling overhead beyond the
# kernel's existing per-program prologue (~1 RDTSC).
#
# Why this beats user-space timing: there is no BPF-internal
# instrumentation to add (no paired bpf_ktime_get_ns() wrappings to
# distort the very thing we're measuring) and no external workload-
# generator vs. baseline subtraction to do. The kernel reports the
# pure cost of running our program on real I/O.
#
# What it does NOT measure: cache/branch-predictor pressure on the
# host kernel code path that surrounds blk_mq_start_request /
# blk_mq_end_request. That second-order effect needs a `perf stat`
# attached-vs-detached comparison over a fio workload (deferred
# follow-up, see D014).
#
# Variants per D014: tries the k=4 default first (covers 5.15-6.12);
# on -E2BIG / -EINVAL from the verifier, falls back to the k=3 variant
# (covers 6.17+). Mirrors the runtime selection in src/iomoments.c.
#
# Usage:
#   sudo scripts/measure_bpf_overhead.sh \
#       [build/iomoments.bpf.o] [build/iomoments-k3.bpf.o]
#
# Requires root for: bpftool prog load (BPF_PROG_TYPE_TRACING
# attach), sysctl write, and dd oflag=direct against /tmp.

set -euo pipefail

BPF_OBJ_DEFAULT=${1:-build/iomoments.bpf.o}
BPF_OBJ_K3=${2:-build/iomoments-k3.bpf.o}
PIN_DIR=/sys/fs/bpf/iomoments_overhead_$$
DD_BLOCKS=${DD_BLOCKS:-20000}
DD_BS=${DD_BS:-4k}

# Default target: a tmpfile on the host's /tmp (a real ext4/xfs path
# on Ed's typical setup). For vmtest runs, the in-VM wrapper sets
# OVERHEAD_TARGET to a loopback block device (/dev/loopN) so dd
# direct-I/O fires real blk_mq events through the loop driver.
if [ -n "${OVERHEAD_TARGET:-}" ]; then
	TMP_FILE="$OVERHEAD_TARGET"
	OVERHEAD_TARGET_OWNED_BY_CALLER=1
else
	TMP_FILE=$(mktemp /tmp/iomoments-overhead.XXXXXX)
	OVERHEAD_TARGET_OWNED_BY_CALLER=0
fi

if [ "$(id -u)" -ne 0 ]; then
	echo "ERROR: must run as root (BPF attach + sysctl + direct I/O)" >&2
	exit 1
fi

for obj in "$BPF_OBJ_DEFAULT" "$BPF_OBJ_K3"; do
	if [ ! -f "$obj" ]; then
		echo "ERROR: BPF object $obj not found. Run 'make bpf-compile' first." >&2
		exit 1
	fi
done

# Use the newest bpftool on the system; older ones miss the JSON
# fields we read. Same pattern as the Makefile's bpf-test-vm recipe.
# shellcheck disable=SC2012
BPFTOOL=$(ls /usr/lib/linux-tools/*/bpftool 2>/dev/null | sort -V | tail -1)
if [ -z "$BPFTOOL" ] || [ ! -x "$BPFTOOL" ]; then
	BPFTOOL=$(command -v bpftool)
fi
if [ -z "$BPFTOOL" ]; then
	echo "ERROR: bpftool not found." >&2
	exit 1
fi

cleanup() {
	rm -rf "$PIN_DIR" 2>/dev/null || true
	# Don't delete a caller-provided target (could be /dev/loopN).
	if [ "$OVERHEAD_TARGET_OWNED_BY_CALLER" -eq 0 ]; then
		rm -f "$TMP_FILE" 2>/dev/null || true
	fi
	rm -f "${LOAD_ERR:-}" 2>/dev/null || true
	# Leave kernel.bpf_stats_enabled at its prior value.
	if [ -n "${PRIOR_STATS:-}" ]; then
		sysctl -wq kernel.bpf_stats_enabled="$PRIOR_STATS" >/dev/null
	fi
}
trap cleanup EXIT

PRIOR_STATS=$(sysctl -n kernel.bpf_stats_enabled)
sysctl -wq kernel.bpf_stats_enabled=1 >/dev/null

mkdir -p "$PIN_DIR"
# Try the k=4 default first; fall back to k=3 on any load failure.
# bpftool exits non-zero on any error including -E2BIG; we don't
# distinguish from shell — if the default fails, retry with k=3 and
# let any second failure propagate the real cause.
LOAD_ERR=$(mktemp /tmp/iomoments-load.err.XXXXXX)
# LOAD_ERR is now visible to the cleanup() trap registered earlier;
# any set -e exit during the case below will rm it.
LOADED_VARIANT=""
LOADED_ORDER=""
# IOMOMENTS_FORCE_VARIANT={k4,k3} skips the try-k4-then-k3 dispatch
# and loads the named variant directly. Used for clean k=4-vs-k=3
# comparisons in the same environment (e.g., both inside vmtest).
case "${IOMOMENTS_FORCE_VARIANT:-}" in
k3)
	"$BPFTOOL" prog loadall "$BPF_OBJ_K3" "$PIN_DIR" autoattach
	LOADED_VARIANT="$BPF_OBJ_K3"
	LOADED_ORDER="k3"
	;;
k4)
	"$BPFTOOL" prog loadall "$BPF_OBJ_DEFAULT" "$PIN_DIR" autoattach
	LOADED_VARIANT="$BPF_OBJ_DEFAULT"
	LOADED_ORDER="k4"
	;;
"")
	if "$BPFTOOL" prog loadall "$BPF_OBJ_DEFAULT" "$PIN_DIR" autoattach 2>"$LOAD_ERR"; then
		LOADED_VARIANT="$BPF_OBJ_DEFAULT"
		LOADED_ORDER="k4"
	else
		echo "default (k=4) variant failed to load — falling back to k=3:" >&2
		cat "$LOAD_ERR" >&2
		rm -rf "$PIN_DIR"
		mkdir -p "$PIN_DIR"
		"$BPFTOOL" prog loadall "$BPF_OBJ_K3" "$PIN_DIR" autoattach
		LOADED_VARIANT="$BPF_OBJ_K3"
		LOADED_ORDER="k3"
	fi
	;;
*)
	echo "ERROR: IOMOMENTS_FORCE_VARIANT must be 'k3' or 'k4' (got '${IOMOMENTS_FORCE_VARIANT:-}')" >&2
	exit 1
	;;
esac

# Snapshot counters before workload.
SNAP_BEFORE=$("$BPFTOOL" prog show -j)

# Generate honest block I/O. oflag=direct bypasses the page cache so
# every write reaches blk_mq → fires both fentry hooks. dd reports
# its own kernel-side throughput; we ignore it and read BPF stats.
dd if=/dev/zero of="$TMP_FILE" bs="$DD_BS" count="$DD_BLOCKS" \
	oflag=direct conv=fsync status=none

# Snapshot counters after.
SNAP_AFTER=$("$BPFTOOL" prog show -j)

# jq is the right tool for the JSON delta — it's standard on every
# distro that ships bpftool and the alternative (regex munging on
# multiline JSON) is wrong by inspection.
if ! command -v jq >/dev/null 2>&1; then
	echo "ERROR: jq required for stats parsing." >&2
	exit 1
fi

# Extract `run_time_ns run_cnt` for `name`. Defaults the kernel
# fields to 0 if jq finds them as null — older kernels expose those
# fields only after the program has run with stats-enabled, and
# some `bpftool prog show -j` versions emit `null` for unobserved
# programs rather than omitting the keys.
extract() {
	echo "$1" | jq -r --arg name "$2" '
		.[] | select(.name == $name) |
		"\(.run_time_ns // 0) \(.run_cnt // 0)"'
}

KERNEL_REL=$(uname -r)
echo "kernel:           $KERNEL_REL"
echo "variant:          $LOADED_VARIANT ($LOADED_ORDER)"
echo

# Sanity-check that bpf_stats_enabled actually took effect before
# we trust the run_time_ns / run_cnt fields. Some kernel + bpftool
# combinations need stats enabled at program-load time, not
# retroactively.
STATS_NOW=$(sysctl -n kernel.bpf_stats_enabled)
if [ "$STATS_NOW" -ne 1 ]; then
	echo "ERROR: kernel.bpf_stats_enabled=$STATS_NOW after sysctl write" >&2
	exit 5
fi

DIAGNOSTIC_DUMPED=0
dump_snapshot_diag() {
	if [ "$DIAGNOSTIC_DUMPED" -eq 1 ]; then
		return
	fi
	DIAGNOSTIC_DUMPED=1
	echo >&2
	echo "Diagnostic: raw bpftool prog show -j entries for our programs:" >&2
	echo "$SNAP_AFTER" | jq -r '
		.[] | select(.name == "iomoments_rq_issue"
			   or .name == "iomoments_rq_complete")
		| "  " + .name + ": " + (. | tostring)' >&2 || true
}

OBSERVED_ANY=0
for prog in iomoments_rq_issue iomoments_rq_complete; do
	BEFORE=$(extract "$SNAP_BEFORE" "$prog")
	AFTER=$(extract "$SNAP_AFTER" "$prog")
	if [ -z "$BEFORE" ] || [ -z "$AFTER" ]; then
		echo "WARN: $prog not found in bpftool snapshot — skipping" >&2
		dump_snapshot_diag
		continue
	fi
	read -r RT_BEFORE CNT_BEFORE <<<"$BEFORE"
	read -r RT_AFTER CNT_AFTER <<<"$AFTER"
	RT_DELTA=$((RT_AFTER - RT_BEFORE))
	CNT_DELTA=$((CNT_AFTER - CNT_BEFORE))
	if [ "$CNT_DELTA" -le 0 ]; then
		echo "$prog: 0 events observed during workload — see diagnostic below" >&2
		dump_snapshot_diag
		continue
	fi
	OBSERVED_ANY=1
	# awk for floating-point division — bash arithmetic is integer-only.
	PER_EVENT_NS=$(awk -v rt="$RT_DELTA" -v c="$CNT_DELTA" \
		'BEGIN { printf "%.2f", rt / c }')
	echo "$prog:"
	echo "  events:           $CNT_DELTA"
	echo "  total_run_time_ns: $RT_DELTA"
	echo "  per_event_ns:     $PER_EVENT_NS"
done

if [ "$OBSERVED_ANY" -eq 0 ]; then
	echo >&2
	echo "ERROR: no per-event measurement was produced." >&2
	echo "  Common causes:" >&2
	echo "  - dd target file ($TMP_FILE) is on tmpfs / overlayfs and" >&2
	echo "    didn't generate blk_mq events. Set TMPDIR=/some/real/disk" >&2
	echo "    and re-run." >&2
	echo "  - kernel.bpf_stats_enabled wasn't honored for tracing progs." >&2
	echo "  - autoattach didn't actually attach the tracepoints. Check" >&2
	echo "    'bpftool prog show' output above for run_cnt > 0." >&2
	exit 6
fi

# Pair symmetry check. Per-block-I/O issue and complete must fire in
# pairs (modulo a few in-flight at workload start/end). A large
# divergence means we're attached to a path that misses a major
# completion route — which is exactly the failure mode that drove
# the 2026-04-26 fentry → tp_btf migration (NVMe batched
# completions silently bypassed blk_mq_end_request).
#
# We require complete count to be at least 50% of issue count.
# Drains at workload boundary and HASH-map eviction account for the
# tolerance; a healthy workload will land much closer to 1:1.
#
# Override for legitimately asymmetric workloads (e.g. a long-running
# load killed mid-flight before its issued requests complete):
#   IOMOMENTS_SKIP_SYMMETRY=1 sudo scripts/measure_bpf_overhead.sh
ISSUE_AFTER=$(echo "$SNAP_AFTER" | jq -r '
	.[] | select(.name == "iomoments_rq_issue") | .run_cnt // 0')
COMPLETE_AFTER=$(echo "$SNAP_AFTER" | jq -r '
	.[] | select(.name == "iomoments_rq_complete") | .run_cnt // 0')
if [ -z "${IOMOMENTS_SKIP_SYMMETRY:-}" ] &&
	[ -n "$ISSUE_AFTER" ] && [ -n "$COMPLETE_AFTER" ] &&
	[ "$ISSUE_AFTER" -gt 100 ]; then
	# 2 * complete >= issue → complete is at least 50% of issue.
	if [ $((COMPLETE_AFTER * 2)) -lt "$ISSUE_AFTER" ]; then
		echo >&2
		echo "ERROR: issue/complete asymmetry — issue=$ISSUE_AFTER," \
			"complete=$COMPLETE_AFTER. Per-event numbers cannot" \
			"be trusted when one side of the pair misses." >&2
		echo "  Likely cause: completion path bypasses our attach" \
			"point. Check whether the kernel uses a path our" \
			"hook doesn't see." >&2
		echo "  Override (asymmetric workload by design):" \
			"IOMOMENTS_SKIP_SYMMETRY=1" >&2
		exit 7
	fi
fi
