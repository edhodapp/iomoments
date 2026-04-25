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

- Linux, kernel ≥ 5.15 (for CO-RE and BTF).
- eBPF program in C (clang `-target bpf`), loaded via libbpf.
- Userspace aggregator in C.
- Python 3.11+ reference implementation used as a numerical oracle in
  the test suite, not shipped.

Architecture targets: `x86_64` and `aarch64`. FreeBSD, macOS, and BSD
classic BPF are explicitly out of scope — those systems have different
tracing primitives (DTrace, the FreeBSD experimental eBPF port) and
would be separate artifacts.

## Algorithm

Online moment updates use Pébay's formulas (Sandia SAND2008-6212), a
generalization of Welford (1962) to arbitrary order with a parallel-
combine rule for merging partial summaries. Per-CPU BPF maps accumulate
partials; userspace merges them to a single aggregate. Moments are
emitted in both raw space and log-space, since I/O latency distributions
are typically log-normal-ish and higher moments in log-space converge
faster and characterize tail shape more directly.

## Status

**Beta-trial-ready as of 2026-04-25.** The arithmetic substrate, the
periodic-drain plumbing, the Level 2 Nyquist diagnostic, and the
D007 Green/Yellow/Amber/Red verdict layer are all live. End-to-end
runs inside vmtest on every kernel in the 5.15 / 6.1 / 6.6 / 6.12
matrix.

**Arithmetic substrate (Level 1):**

- **`src/pebay.h`** — userspace-canonical Pébay k=4 running summary
  in `double`. Mean, variance, skewness, excess kurtosis from one
  online update; parallel-combine merge for per-CPU aggregation.
  Property-tested against textbook fixtures and scipy.
- **`src/u128.h`** — hand-rolled 128-bit signed integer arithmetic
  out of 64-bit primitives (BPF verifier rejects compiler-emitted
  `__multi3` / `__divti3` libcalls). `s128_add/sub`,
  `s64×s64→s128`, `s128_mul_u64/s64`, `s128_div_u64` (Knuth
  Algorithm D / Hacker's Delight 9-3), `s128_to_double`.
  Validated against `__int128` over hand-picked boundary cases +
  2000-trial deterministic LCG sweeps per primitive.
- **`src/pebay_bpf.h`** — BPF-safe fixed-point counterpart at k=4.
  Q32.32 signed-ns running mean, int64 ns² m2, s128 m3 (ns³) and
  s128 m4 (ns⁴). Round-trip-tested against `pebay.h` for mean,
  variance, skewness, and excess kurtosis with documented
  tolerances per fixture. m2 saturation at HDD-σ workloads is
  documented; userspace periodic drain mitigates.
- **`src/iomoments.bpf.c`** — BPF program attached via fentry on
  `blk_mq_start_request` (issue) and `blk_mq_end_request`
  (complete). Real I/O-latency measurement; verifier-accepted on
  the full 5.15 / 6.1 / 6.6 / 6.12 matrix.

**Periodic drain + Level 2 (D013):**

- **`src/iomoments.c`** — userspace loader. Periodically drains
  + resets the per-CPU map every `--window` ms (default 100ms),
  pushes each windowed snapshot onto a time-indexed ring,
  aggregates the ring into the global summary at end-of-duration.
- **`src/iomoments_level2.h`** — D013 moments-of-moments analysis.
  Takes the time series of windowed `iomoments_summary` snapshots
  and computes variance of the windowed-mean stream, the
  CLT-predicted variance under stationary Nyquist-met assumptions
  (σ²/n_per_window), the variance ratio, the Nyquist-confidence
  fingerprint `exp(-½·(log₂ ratio)²)`, and lag-{1,2,4,8}
  autocorrelation of windowed means. Aliasing presents as variance
  ratio < 1 (windowed means insensitive to phase); non-stationarity
  presents as ratio > 1.
- **`tests/c/test_level2.c`** — synthetic-window fixtures
  (deterministic LCG-driven Box-Muller Gaussian) covering
  stationary high-confidence, drifting-mean low-confidence, and
  aliased-periodic dip cases.

**D007 verdict layer:**

- **`src/iomoments_verdict.h`** — six signal evaluators
  (sample_count, variance_sanity, kurtosis_sanity,
  nyquist_confidence, autocorr_residual, half_split_stability),
  each emitting a Green/Yellow/Amber/Red status with a short
  rationale string. Overall verdict is the worst-of-all. RED
  refuses the moment-based summary and recommends DDSketch / HDR
  Histogram as alternatives.
- **`tests/c/test_verdict.c`** — 17 fixtures covering each signal
  evaluator's threshold bands, the worst-of-all aggregation
  monotonicity, and an end-to-end stationary-Gaussian → GREEN
  scenario.

**Test, build, gate:**

- **`tooling/src/iomoments_ontology/`** — Pydantic-typed
  formal-requirements DAG. Audit gate fires on every push.
- **`tooling/src/audit_ontology/`** — cross-references every
  constraint's `implementation_refs` / `verification_refs`
  against the committed code. Pre-push + CI.
- **kernel-matrix VM testing** — every push exercises
  `iomoments.bpf.o` against four vmtest-built guest kernels
  (5.15 / 6.1 / 6.6 / 6.12, fedora38 preset) via
  `make bpf-test-vm-matrix`. D012 guarantees iomoments never
  loads BPF on the host kernel.

### Running iomoments

```
make iomoments-build
sudo ./build/iomoments --duration=10 --window=100
```

The report emits Level 1 moments, Level 2 Nyquist diagnostic, and
the D007 verdict with per-signal breakdown.

### Still to land (post-beta)

D007 names additional diagnostic signals not yet implemented:

- **Carleman partial sum** and **Hankel matrix conditioning** —
  moment-determinacy conditions computed directly from m2/m3/m4.
  No new infrastructure needed; layer on top of the existing
  verdict evaluators.
- **Hill tail-index estimator** — needs an order-statistic
  reservoir (top-k samples) in BPF or via ringbuf.
- **KS goodness-of-fit to log-normal** — needs an empirical CDF
  (histogram or sample reservoir).
- **Spectral flatness sweep** — sweeps window length at userspace
  analysis time. Reuses the existing window ring; no BPF change.

Design log: **`DECISIONS.md`** (D001–D013). CD-pipeline design
lives in **`CD-PIPELINE-PROPOSAL.md`** (some sections superseded
by D008/D009/D010/D012; supersession banners at the top point
forward).

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
