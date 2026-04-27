# iomoments BPF program perf history

Append-only log of measured per-event overhead from
`scripts/measure_bpf_overhead.sh`. Per CLAUDE.md: "Maintain a perf
stats log."

Each entry records:
- **commit** — git short hash of the measured tree.
- **kernel** — `uname -r` of the host running the measurement.
- **variant** — which BPF object loaded (k4 / k3, see D014).
- **CPU** — model, base GHz from `/proc/cpuinfo`. Used to convert
  per-event ns into per-event cycles.
- **workload** — what `dd` invocation generated the load.
- **measurements** — per-program `events`, `total_run_time_ns`, and
  derived `per_event_ns`.

Methodology: kernel BPF stats (`sysctl kernel.bpf_stats_enabled=1`)
report cumulative `run_time_ns` and `run_cnt` per program; we sample
before/after a workload and divide. See the script's header comment
for the rationale on why this beats user-space wrappings.

Pair-symmetry gate: the script aborts if `complete < issue / 2`.
Issue and complete fire in pairs on a healthy workload; a wide
divergence means the BPF attach is missing a completion path (the
failure mode that drove the 2026-04-26 fentry → tp_btf migration).
Recording asymmetric numbers would mislead. Override
`IOMOMENTS_SKIP_SYMMETRY=1` for workloads that are legitimately
asymmetric by design (mid-flight kill, unbounded outstanding I/O).

What this measures: pure cost of running the iomoments BPF program
inside `iomoments_rq_complete` and `iomoments_rq_issue` per blk_mq
event. What it does NOT measure: cache / branch-predictor pressure
on surrounding host kernel paths (deferred; see D014's outer-loop
test layer).

## Entries

### 2026-04-26 — first baseline (k=3 variant, NVMe, Ubuntu 6.17)

- **commit:** `2c27e4e` (k=3 fallback variant landed; pre-#49 fix)
  followed by the in-progress `tp_btf` migration that unlocked the
  honest measurement.
- **kernel:** `6.17.0-20-generic` (Ubuntu host; rejects k=4 with
  E2BIG, k=3 loads).
- **CPU:** Intel Core i7-8550U @ 1.80 GHz base (Whiskey Lake, 8
  logical cores).
- **storage:** Toshiba KXG50ZNV512G NVMe SSD (the device whose
  batched-completion path drove the #49 fix; tp_btf attach now
  catches every completion).
- **variant:** `iomoments-k3.bpf.o` (k=3 — m4 update absent;
  m4-dependent diagnostic signals report YELLOW per D014).
- **workload:** `dd if=/dev/zero of=/tmp/<f> bs=4k count=20000
  oflag=direct conv=fsync`.
- **methodology:** `kernel.bpf_stats_enabled=1`; before/after
  snapshots of `bpftool prog show -j`'s `run_time_ns` and `run_cnt`;
  per-event ns = Δrun_time_ns / Δrun_cnt. Full script:
  `scripts/measure_bpf_overhead.sh`.

| program | events | total_run_time_ns | per_event_ns |
|---|---|---|---|
| `iomoments_rq_issue`    | 20,018 |  8,437,628 | **421.50** |
| `iomoments_rq_complete` | 20,019 | 10,868,382 | **542.90** |

**Combined per-event cost (issue + complete pair):** 964.40 ns.

**Pair symmetry:** issue 20,018 / complete 20,019 — 0.005% drift,
well inside the 50% asymmetry threshold the script enforces.
Confirms the `tp_btf` migration captures the NVMe batched
completion path correctly.

**Caveat — what these numbers include:** the kernel's per-program
stats accounting (one `ktime_get_ns` call before and after each
program run) is part of the measured cost. On Whiskey Lake at
1.8 GHz base, that overhead is roughly 50-100 ns/event. Our
program's actual work (map lookup, ktime, the issue-side map
update *or* the complete-side k=3 Pébay update + topk insert) is
the remainder.

**Caveat — variant scope:** these numbers are for the **k=3**
variant (m4 update absent). The k=4 variant on a 5.15-6.12 kernel
would add ~4 divides and ~3 multiplies to the complete path. A
follow-up measurement on a kernel that accepts k=4 should record
the k=4 cost separately for comparison.

**Caveat — what these numbers do NOT include:** cache /
branch-predictor pressure on surrounding host kernel paths. That
second-order effect requires a `perf stat` attached-vs-detached
comparison over a fio workload, which is deferred per D014's
outer-loop test layer.

### 2026-04-26 — k=4 vs k=3 comparison inside vmtest v6.12

Same host CPU and same vmtest+loopback environment for both runs;
only the BPF variant differs. Lets us isolate the marginal cost of
the m4 update body cleanly.

- **commit:** `c6e4432` (tp_btf migration; honest measurement
  infrastructure).
- **kernel:** vmtest fedora38-config v6.12 (rebuilt 2026-04-26 with
  `BLK_DEV_LOOP=y` so the in-VM script could set up a tmpfs-backed
  loopback block device for honest blk_mq events).
- **environment:** vmtest guest under KVM on Ed's Whiskey Lake
  laptop; loopback storage backed by 4 KB writes against
  `/dev/loop0` (tmpfs file).
- **methodology:** `scripts/measure_bpf_overhead_in_vm.sh
  ~/kernel-images/vmlinuz-v6.12`. For the k=3 run, set
  `IOMOMENTS_FORCE_VARIANT=k3`. Otherwise identical.

| variant | events | issue ns | complete ns | combined ns |
|---|---|---|---|---|
| k=4 | 20,001 / 20,002 | 511.41 | 1037.68 | **1549.09** |
| k=3 | 20,001 / 20,002 | 534.51 |  817.41 | **1351.92** |

**Marginal cost of m4 update body** (complete-side, k=4 minus k=3):
**~220 ns/event**. That's the per-sample cost of the four s128
divisions and three s128 multiplications the m4 update adds on top
of the m3/m2/m1 path. Within the order-of-magnitude expected from
the shift-subtract divide's 64-iteration body and BPF's per-helper-
call overhead.

**Issue-side k=4 vs k=3 delta** is 23 ns, not zero. The issue path
is *identical* between variants (only the complete body differs);
this reflects environmental noise (KVM scheduling, page-cache
state, vmtest startup transients). Treat it as the floor on the
measurement's precision in this environment — anything below that
delta isn't a real signal.

**Pair symmetry on both runs:** 20,001 issues / 20,002 completes
(0.005% drift). The tp_btf attach catches the loopback driver's
completion path cleanly.

**Comparison to the host-NVMe k=3 baseline above:** the same k=3
variant ran 542.90 ns/event complete on the host's real NVMe; the
vmtest+loopback run shows 817.41 ns/event complete. ~275 ns gap,
attributable to KVM passthrough + virtio + loopback driver
overhead (different kernel scaffolding around the same BPF
program). Both numbers are honest within their environment.
