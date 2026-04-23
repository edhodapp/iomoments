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

Pre-code. The design log is in `DECISIONS.md`. The build and quality-
gate pipeline design is in `CD-PIPELINE-PROPOSAL.md`. No source code,
no repository yet — this is the design-crystallization stage.

A progress section will be added once implementation begins.

## License and contributions

AGPL-3.0-or-later. See `LICENSE` for the full license text and
`COPYRIGHT` for the project notice, contribution policy, and commercial-
licensing contact path.

The BPF program source file (`src/iomoments.bpf.c`, once written) will
be dual-licensed `(GPL-2.0-only OR AGPL-3.0-or-later)` for kernel-ABI
reasons (the kernel's `license_is_gpl_compatible()` allowlist does not
recognize the literal string "AGPL"). Rationale in `DECISIONS.md` D001.

External contributions are not accepted. Bug reports via GitHub issues
are welcome; fixes described in issues may be reimplemented by the
author. See `COPYRIGHT` for the rationale.
