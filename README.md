# iomoments

An eBPF-based I/O latency shape characterization tool for Linux. Reports
moments of latency distributions — mean, variance, skewness, kurtosis,
and higher — **together with an explicit feasibility verdict** that says
when those moments are a trustworthy summary of the observed workload
and when they are not.

## Thesis

There are two questions one can ask about the moments of a distribution.

The classical moment problem (Hamburger 1920, Stieltjes 1894, Hausdorff
1921) asks whether a distribution can be *reconstructed* from its
moments. In general it cannot — log-normal distributions are the
canonical counterexample — and this is the question mathematicians
rightly flinch at when they see moment-based tools for shape analysis.

iomoments asks the engineering question instead: *given N moments plus
a diagnostic battery, can we emit a compact shape summary with a stated
validity domain?* For well-behaved workloads (stationary, tail index
above the requested moment order, moment-determinate) the answer is
yes. For heavy-tailed, aliased, or non-stationary workloads the answer
is no — and iomoments says so, out loud, on every run.

The diagnostic layer is the product. Features that strengthen it are
core; features that emit more numbers without improving validity
reporting are out of scope; features that remove verdicts to "simplify
the output" are rejected.

Full design rationale and the motivation behind the diagnostic stance:
**<https://hodapp.com/posts/honest-moments/>**.

## Output categories

Every run reports one of four verdicts alongside the numerical moments:

- **Green** — moments are a trustworthy shape summary for this
  workload. Emitted with expected error budget.
- **Yellow** — moments are informative but miss some structure (e.g.,
  bimodality). Emitted with caveats.
- **Amber** — moments are likely biased (e.g., aliasing suspected).
  Emitted with a diagnostic recommendation.
- **Red** — moments are the wrong primitive for this workload (e.g.,
  heavy tail with non-existent variance). Moment-based summary is
  **refused**; an alternative tool (DDSketch, HDR Histogram) is
  recommended.

## Platform

<!-- claim -->Linux. In the vmtest matrix, the default k=4 program
loads on kernels 5.15, 6.1, 6.6, and 6.12 and is rejected by the
verifier on 6.17 and 6.18; the k=3 fallback loads on the full
5.15 / 6.1 / 6.6 / 6.12 / 6.17 / 6.18 set. Intermediate kernels
(6.13–6.16) are untested.<!-- /claim --> The k=4 → k=3 boundary is the BPF
verifier's 1M-step instruction-tracking budget, which kernels v6.17
and later enforce more aggressively against the k=4 program's multi-
precision arithmetic path explosion. iomoments selects the variant
at runtime: it tries k=4 first, falls back to k=3 on verifier
rejection, and reports YELLOW with "insufficient-moment-order on
this kernel" rationale on m4-dependent diagnostic signals when k=3
is loaded.

<!-- claim -->BPF program attached via tp_btf to the
``block_rq_issue`` and ``block_rq_complete`` tracepoints.<!-- /claim -->
Both fire per-request from every block-layer completion path,
including the batched-completion path used by NVMe (this matters —
the prior fentry attach to ``blk_mq_end_request`` silently bypassed
the batch path, undercounting NVMe completions by ~99.99%; see
``src/iomoments.bpf.c`` header comment for the migration rationale).

- eBPF program in C (clang `-target bpf`), loaded via libbpf.
- Userspace aggregator in C.
- Python 3.11+ reference implementation used as a numerical oracle in
  the test suite, not shipped.

Architecture targets: `x86_64` (verified end-to-end) and `aarch64`
(BPF program builds; in-vmtest verification per arch is on the
roadmap). FreeBSD, macOS, and BSD classic BPF are explicitly out of
scope — those systems have different tracing primitives (DTrace, the
FreeBSD experimental eBPF port) and would be separate artifacts.

## Algorithm

Online moment updates use Pébay's formulas (Sandia SAND2008-6212), a
generalization of Welford (1962) to arbitrary order with a parallel-
combine rule for merging partial summaries. Per-CPU BPF maps accumulate
partials; userspace merges them to a single aggregate. Moments are
emitted in both raw space and log-space, since I/O latency distributions
are typically log-normal-ish and higher moments in log-space converge
faster and characterize tail shape more directly.

## Status

<!-- claim -->Beta-trial-ready as of 2026-04-28.<!-- /claim --> The
arithmetic substrate, the periodic-drain plumbing, the Level 2
Nyquist diagnostic, the full D007 verdict layer (twelve diagnostic
signals), the k=3 fallback variant, the cross-kernel BPF verifier
matrix (5.15–6.18 in vmtest plus three live cloud distros), and the
formal-requirements audit gate are all live. End-to-end runs inside
vmtest pass on every kernel in the supported matrix.

**Arithmetic substrate (Level 1):**

- **`src/pebay.h`** — userspace-canonical Pébay k=4 running summary
  in `double`. Mean, variance, skewness, excess kurtosis from one
  online update; parallel-combine merge for per-CPU aggregation.
- **`src/u128.h`** — hand-rolled 128-bit signed integer arithmetic
  out of 64-bit primitives (BPF verifier rejects compiler-emitted
  `__multi3` / `__divti3` libcalls). `s128_add/sub`,
  `s64×s64→s128`, `s128_mul_u64/s64`, `s128_div_u64` (Knuth
  Algorithm D / Hacker's Delight 9-3), `s128_to_double`.
- **`src/pebay_bpf.h`** — BPF-safe fixed-point counterpart at k=4.
  Q32.32 signed-ns running mean, int64 ns² m2, s128 m3 (ns³) and
  s128 m4 (ns⁴). m2 saturation at HDD-σ workloads is documented;
  userspace periodic drain mitigates.
- **`src/iomoments.bpf.c`** — BPF program attached via tp_btf to
  ``block_rq_issue`` and ``block_rq_complete``. Real I/O-latency
  measurement; verifier-accepted on the full 5.15 / 6.1 / 6.6 /
  6.12 supported range, with the k=3 fallback covering 6.17/6.18
  where the k=4 program exhausts the verifier complexity budget.

**Periodic drain + Level 2 (D013):**

- **`src/iomoments.c`** — userspace loader. Periodically drains
  + resets the per-CPU map every `--window` ms (default 100ms),
  pushes each windowed snapshot onto a time-indexed ring,
  aggregates the ring into the global summary at end-of-duration.
- **`src/iomoments_level2.h`** — D013 moments-of-moments analysis.
  Variance of windowed-mean stream vs CLT prediction, Nyquist-
  confidence fingerprint `exp(-½·(log₂ ratio)²)`, lag-{1,2,4,8}
  autocorrelation. Aliasing presents as ratio < 1; non-stationarity
  as ratio > 1.

**D007 verdict layer (twelve signals shipped):**

- **`src/iomoments_verdict.h`** — sample_count, variance_sanity,
  kurtosis_sanity, **carleman_partial_sum**, **hankel_condition_number**,
  **hill_tail_index**, **jb_normality**, **edgeworth_pdf_consistency**,
  **half_split_moment_stability**, nyquist_confidence,
  autocorr_residual, **spectral_flatness_sweep**. Each emits a
  Green/Yellow/Amber/Red status with a short rationale. Overall
  verdict is the worst-of-all. RED refuses the moment-based summary
  and recommends DDSketch / HDR Histogram as alternatives.
- **`src/iomoments_topk.h`** — order-statistic reservoir (K=32) for
  the Hill tail-index estimator, maintained per-CPU in BPF.

**Test, build, gate (D008 / D009 / D010 / D015):**

- <!-- claim -->Four-engine C static analysis pre-push:
  compile-as-lint (gcc + clang), clang-tidy, cppcheck,
  scan-build.<!-- /claim -->
- **`tooling/src/iomoments_ontology/`** — Pydantic-typed
  formal-requirements DAG.
- **`tooling/src/audit_ontology/`** — cross-references every
  constraint's `implementation_refs` / `verification_refs`
  against the working tree, plus the D015 freshness check (was a
  TestResult captured at-or-after the impl's last edit, in every
  required environment?). Five wired producers (pytest, AWS
  probe, C-test, vmtest matrix, perf measurement) feed the
  test-results DAG. <!-- claim -->Pre-push refuses pushes where any
  covered tested-status claim has stale or missing TestResults.<!-- /claim -->
  PerformanceConstraint budget enforcement is a roadmap item not yet
  wired into the gate (see Roadmap below).
- **kernel-matrix VM testing** — every push exercises
  `iomoments.bpf.o` and `iomoments-k3.bpf.o` against six
  vmtest-built guest kernels (5.15 / 6.1 / 6.6 / 6.12 / 6.17 /
  6.18) via `make bpf-test-vm-matrix`.
- **cloud-faithfulness probe** (D016) — `scripts/aws_tracer.sh`
  loads the BPF programs against three live AWS distros (Ubuntu
  20.04 = 5.15 floor witness, Ubuntu 22.04 = 6.8 HWE, Amazon
  Linux 2023 = 6.18) and verifies vmtest predictions hold under
  vendor-patched cloud kernels.

## Installation

### Build dependencies

<!-- claim -->Required system packages on Ubuntu 24.04 LTS (the
reference build target — Python 3.12 is the system default, and
the kernel-tools metapackage names are Ubuntu-specific):<!-- /claim -->

```
sudo apt install \
    clang libbpf-dev linux-libc-dev \
    linux-tools-common linux-tools-generic \
    make python3-venv
```

For the full developer pipeline (tests + four-engine C lint + ontology
audit) also install:

```
sudo apt install \
    gcc clang-tools cppcheck clang-format \
    shellcheck
```

Other distributions (Debian, Fedora, Arch) are expected to work,
but the kernel-tools package names differ (e.g., `linux-perf` on
Debian) and Python 3.11+ may need a non-default install. Distro-
specific build recipes are not maintained.

### Build

```
make iomoments-build
```

Produces `build/iomoments` (userspace binary) and the two BPF
objects `build/iomoments.bpf.o` (k=4) and
`build/iomoments-k3.bpf.o` (k=3 fallback).

### Run

```
sudo ./build/iomoments --duration=10 --window=100
```

`sudo` is required for BPF program load + tracepoint attach. The
report emits Level 1 moments, Level 2 Nyquist diagnostic, and the
D007 verdict with per-signal breakdown.

## Roadmap (post-ship)

- **Calibration validation against real workloads.** Verdict
  thresholds (Carleman ratio bands, Hill α boundaries, JB cutoffs)
  were chosen from theory + small synthetic fixtures. Validating
  them against actual NVMe / SSD / HDD / NFS workloads with
  known-shape distributions is the highest-leverage open work.
- **#47 cloud matrix orchestrator.** Periodic re-runs of the AWS
  probe across additional distros (RHEL, Debian, Fedora) so
  vendor-kernel drift doesn't catch us by surprise.
- **PerformanceConstraint budget enforcement in the audit gate.**
  D015 captures perf measurements but doesn't yet compare them
  against `probe_phase_overhead` etc. budgets.

Design log: **`DECISIONS.md`** (D001 through D016).

## License and contributions

AGPL-3.0-or-later. See `LICENSE` for the full license text and
`COPYRIGHT` for the project notice, contribution policy, and commercial-
licensing contact path.

The BPF program source file (`src/iomoments.bpf.c`) is dual-licensed
`(GPL-2.0-only OR AGPL-3.0-or-later)` for kernel-ABI reasons (the
kernel's `license_is_gpl_compatible()` allowlist does not recognize
the literal string "AGPL"). Rationale in `DECISIONS.md` D001.

External contributions are not accepted. Bug reports via GitHub issues
are welcome; fixes described in issues may be reimplemented by the
author. See `COPYRIGHT` for the rationale.
