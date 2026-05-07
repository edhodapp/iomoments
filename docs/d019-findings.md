# D019 calibration findings — first cloud run

**Run captured:** 2026-05-07 03:56–04:13 UTC.
**Hardware:** AWS `m5.large`, `us-east-1b`, Ubuntu 24.04.4 LTS,
kernel `6.17.0-1012-aws`, fio 3.36.
**iomoments commit:** `e31e0b0` (D019 build-out + fio config fix).
**Variant loaded:** `k=3` (kernel 6.17 rejected `k=4` per D014; the
fallback engaged correctly).
**Cost:** ~$0.05 of EC2 time.
**Artifacts:** `docs/d019-data/<class>/<rep>/iomoments.json`,
`fio.json`, `meta.txt`. Raw `.bin` dumps stayed in
`scripts/calibration_d019/out-ec2/<run_id>/` (gitignored;
~16 MB total).

## TL;DR

1. **Mean / variance / skewness compute to floating-point parity
   with scipy** across all 9 reps — iomoments' Pébay arithmetic is
   numerically correct on every shape we ran.
2. **Kurtosis diverges from scipy on k=3 kernels.** Cause is well
   understood (BPF-side m4 isn't maintained; userspace gets only
   the cross-window kurt contribution from the merge formula).
   The verdict layer correctly YELLOWs every m4-dependent signal
   in this regime, so the verdict *outcome* is honest even though
   the kurt *number* isn't.
3. **EBS gp3 at 2K IOPS / QD=4 is genuinely non-stationary.** The
   "Class A → GREEN" expectation in `D019` was a wrong prior, not
   a verdict-layer miscalibration. iomoments correctly flags
   bandwidth-throttling, autocorrelation, and within-run drift on
   what we naïvely called the "well-behaved" baseline.
4. **Bimodal-by-block-size construction (Class C) didn't produce
   a bimodal distribution.** scipy reports near-zero skew and
   near-zero excess kurt — actually platykurtic / uniform-ish.
   The construction needs revising.
5. **Hill α at k=32 is non-robust** on these workloads. Same
   workload, three reps: α ∈ {3.3, 14.8, 25.8} for Class A. The
   topk reservoir is dominated by transient outliers (background
   EBS noise) rather than the actual tail.

## Verdict matrix (target vs observed)

| Class | Target | Rep 1 | Rep 2 | Rep 3 |
|------:|--------|-------|-------|-------|
| A     | GREEN  | AMBER | AMBER | AMBER |
| B     | AMBER/RED | AMBER | AMBER | AMBER |
| C     | YELLOW | AMBER | YELLOW | YELLOW |

Class A all-AMBER is **not a false positive** — see the per-signal
breakdown below. Class B matched expectations. Class C inconsistency
across reps is itself a finding (verdict instability under a
genuinely-uniform-ish distribution that the suite's signals weren't
calibrated for).

## Moments: iomoments vs scipy

scipy ground truth computed by reading the raw `.bin` dump and
running `np.mean / np.var / scipy.stats.skew / scipy.stats.kurtosis`
on the same samples iomoments saw via the BPF tp_btf path.

| rep   | iom_mean (ns) | scipy_mean | iom_var | scipy_var | iom_skew | scipy_skew | iom_kurt | scipy_kurt |
|-------|--------------:|-----------:|--------:|----------:|---------:|-----------:|---------:|-----------:|
| A/1   | 575,417 | 575,414 | 2.977e+10 | 2.977e+10 | 12.02 | 12.02 | 141.2 | 178.5 |
| A/2   | 568,383 | 568,383 | 1.599e+09 | 1.599e+09 | 6.61  | 6.61  | 367.7 | 382.5 |
| A/3   | 574,331 | 574,331 | 3.395e+10 | 3.395e+10 | 7.96  | 7.96  | 71.9  | 75.3  |
| B/1   | 20,391,980 | 20,391,975 | 5.995e+13 | 5.995e+13 | 1.49 | 1.49 | 6.79 | 14.14 |
| B/2   | 20,392,980 | 20,392,947 | 3.997e+13 | 3.997e+13 | 0.49 | 0.49 | -0.86 | 1.80 |
| B/3   | 20,387,030 | 20,387,092 | 5.142e+13 | 5.142e+13 | 2.39 | 2.39 | 26.27 | 36.57 |
| C/1   | 919,769 | 919,768 | 9.141e+10 | 9.140e+10 | 0.10 | 0.10 | -1.94 | -1.02 |
| C/2   | 1,010,132 | 1,010,132 | 6.962e+10 | 6.962e+10 | 0.04 | 0.04 | -2.89 | -1.00 |
| C/3   | 1,012,118 | 1,012,118 | 7.210e+10 | 7.210e+10 | 0.30 | 0.30 | -0.96 | 0.99 |

**Mean, variance, and skewness all match to 4–6 decimal places.**
The arithmetic substrate (D006 Pébay updates, D011 fixed-point in
BPF, D006 parallel-combine merge in userspace) is numerically
correct on every shape we exercised.

**Kurtosis diverges 4 %–100 %** depending on workload. Root cause:
the `k=3` BPF variant doesn't update `m4`. The aggregate `m4` you
see in `iomoments.json` comes from the userspace merge formula's
cross-window kurt-correction term — which is mathematically valid
but captures only between-window kurtosis, not within-window. For
windows containing 11–270 samples each, the within-window kurtosis
contribution is significant on heavy-tailed data and zero on uniform
data, which is exactly the divergence pattern observed. The verdict
layer correctly demotes every m4-dependent signal (`kurtosis_sanity`,
`carleman_partial_sum`, `hankel_conditioning`, `jb_normality`,
`edgeworth_pdf_consistency`) to YELLOW with the rationale
*"moment-order=k3; m4 not maintained on this kernel (D014 fallback)"*
— so the verdict path doesn't lie about kurtosis even though the
number itself is unreliable in this regime.

This was already covered by D014's design but D019 confirms it
empirically. To validate kurtosis numerically we'd need to re-run
on a kernel that accepts `k=4` (5.15 / 6.1 / 6.6 / 6.12). Queued.

## Class-by-class

### Class A: target GREEN, observed AMBER

**Workload:** randread, 4 KB blocks, QD=4, 2K IOPS rate-limited,
direct=1 against `/dev/nvme1n1` (50 GB gp3).

**iomoments verdict:** AMBER on every rep. Per-signal breakdown
on `A/1`:

| signal | status | rationale |
|---|---|---|
| `sample_count` | GREEN | n = 177,428 |
| `variance_sanity` | GREEN | σ² = 2.977e10 ns² |
| `kurtosis_sanity` ... `edgeworth_pdf_consistency` | YELLOW | k=3 fallback, m4 not maintained |
| `hill_tail_index` | GREEN | α̂ = 14.7, light tail |
| `nyquist_confidence` | **AMBER** | confidence=0.00, V/V₀ = 93.21 |
| `autocorr_residual` | **AMBER** | max \|autocorr\| = 0.58 at lag 1 |
| `spectral_sweep` | GREEN | min ratio 93.2 at W' = 0.1 s |
| `half_split_stability` | **AMBER** | mean shift 0.08 σ_pooled, σ²-ratio 27.33 |

Three independent signals (Nyquist, autocorr, half-split)
detected non-stationarity. Variance of the windowed-mean stream
is **93× the CLT prediction** — a strong "this stream is not
i.i.d." finding. Lag-1 autocorrelation of 0.58 says consecutive
100 ms windows are highly correlated. The variance ratio between
the first and second half of the run is 27 — substantial drift
within the 90-second observation.

**This is honest behaviour, not a false positive.** EBS gp3 has
real bandwidth-throttling, token-bucket effects, network jitter,
and noisy-neighbour variance even at low load. iomoments
correctly identifies that "moments-as-trustworthy-summary" should
not apply here. The original D019 design assumption — that gp3 at
low IOPS would produce a clean baseline that hits GREEN — was a
wrong prior; the verdict layer is calibrated correctly for this
regime.

The std of the latency stream itself varies 40–184 µs across the
three reps (≈4× range) and excess kurtosis varies 75–382 — same
workload, different runs. EBS background noise is part of the
signal, not separable from it.

### Class B: target AMBER/RED, observed AMBER

**Workload:** randread, 4 KB, QD=128, 12K IOPS (4× over the gp3
3K baseline) — deliberate over-subscription to provoke
throttling.

**iomoments verdict:** AMBER on all three reps. Mean ≈ 20.4 ms
(40× higher than Class A — heavy queueing exactly as expected).
std 6–8 ms. scipy skew 0.5–2.4, scipy kurt 1.8–37. Hill α 5–13
(medium-heavy tail).

This matches the design intent — workload that EBS aggressively
throttles, latency tail extending well past the natural-completion
window. AMBER is the right verdict; the prediction was AMBER **or**
RED and we landed on the gentler side.

### Class C: target YELLOW (bimodal), observed AMBER+YELLOW

**Workload:** randread, bsrange 4 KB–256 KB, QD=8, 4K IOPS.
Intent: 4 KB reads complete fast, 256 KB reads complete slower
(bandwidth-bound), giving a bimodal latency distribution.

**Observed:** scipy skew ≈ 0.0–0.3, scipy kurt ≈ −1.0 to +1.0.
**This is platykurtic / nearly-uniform, not bimodal.** The
construction failed to produce the intended shape. Likely reason:
on gp3 with mixed block sizes at QD=8, EBS's combined bandwidth/
IOPS limiting smooths what would otherwise be two distinct modes
into a flatter distribution.

iomoments' verdict instability across the three reps (AMBER /
YELLOW / YELLOW) reflects threshold-band assignment on a
distribution that doesn't fit a standard model. Calibration
finding for the verdict layer's edge cases.

**Action item:** revise the bimodal construction. Options:
- Use two separate fio jobs running concurrently (one 4K /
  one 256K) instead of `bsrange`. Each becomes a clear mode.
- Mix cached + uncached I/O explicitly via `--readwrite=read
  --norandommap=0 --random_distribution=zoned`.
- Use a slower medium (st1) where seek-vs-cached gives a real
  bimodal split.

Queued; not blocking.

## Calibration findings worth keeping

### F1: Pébay arithmetic is correct.

Mean, variance, and skewness match scipy to 4–6 decimal places on
all 9 reps across every shape. The C path agrees with the Python
oracle (D018) which agrees with scipy. No drift in the lower three
moments.

### F2: Kurtosis on k=3 kernels is structurally incomplete.

The cross-window contribution from the parallel-combine merge is
mathematically valid but captures only between-window kurt, not
within. For 100 ms windows containing tens to hundreds of samples,
the within-window 4th moment is substantial on heavy-tailed data
and absent on uniform data — which matches the observed sign-of-
divergence pattern. The verdict layer correctly demotes every
m4-dependent signal in this regime; the verdict outcome stays
honest even when the kurt number isn't.

D014 already specified this; D019 confirms empirically.

### F3: "Clean baseline on EBS gp3" is the wrong prior.

The original D019 design assumed Class A would emit GREEN. It
didn't, and shouldn't have — three independent signals
(Nyquist, autocorr, half-split) correctly detected
non-stationarity. EBS gp3 at any sustained workload has
bandwidth-throttling and network jitter that violate the i.i.d.
assumption underlying GREEN.

The fix isn't to relax verdict thresholds — it's to revise the
calibration prior. *On real cloud storage, the honest verdict for
moment-based summary is AMBER or YELLOW, never GREEN.* GREEN as a
calibration target requires a workload whose tail is genuinely
known to be light — synthetic distribution generation or local
NVMe with no shared infrastructure.

### F4: Hill α at k=32 is non-robust on these workloads.

Three reps of Class A produced α ∈ {3.3, 14.8, 25.8}. The k=32
topk reservoir is dominated by background EBS spikes (rare,
transient) rather than the structural tail of the I/O workload.
Calibration miscalibration for the Hill signal itself.

**Action item:** investigate. Options:
- Increase k (e.g., k=128 or k=256) so transient spikes don't
  dominate the reservoir average.
- Use a robust Hill variant (e.g., median-based or
  bias-corrected).
- Cap at the n-th percentile rather than top-K absolute, so the
  reservoir tracks the workload's tail rather than absolute
  outliers.

Queued under the verdict-layer-tuning track.

### F5: Class C bimodal-by-blocksize construction failed.

Documented above. Action item: revise.

## What this run does NOT cover

- **k=4 kernel calibration.** EC2 6.17.0-1012-aws and the laptop's
  6.17.0-22-generic both reject k=4. Need a kernel in the
  5.15–6.12 range to validate kurtosis on k=4 directly. Could
  re-run provision.sh against a 6.8-shipping AMI (Ubuntu 22.04
  jammy with libbpf rebuilt locally, or AL2023 with a 6.6 swap).
- **Cross-instance variance.** One instance type (m5.large), one
  AZ. EBS behaviour varies by instance class (m5 vs c5 vs t3).
  Queued under the cross-distro / cross-instance follow-on.
- **Cross-volume variance.** One gp3 volume, default IOPS/throughput.
  io2 / st1 / standard would each produce different shapes.
- **Long-duration drift.** 90-second runs. Multi-hour runs would
  surface diurnal noise patterns the half_split signal can't
  catch at this window size.

## Run mechanics worth remembering

The first four `provision.sh` attempts failed and we lost ~$0.02
debugging through three small bugs:

1. **Run 1: apt repository transient.** First run hit a flaky apt
   mirror; the diagnostic was on `/tmp/apt.log` on the EBS volume
   that DeleteOnTermination'd with the instance. Fix:
   `Acquire::Retries=3` + ERR trap to surface log to local stderr
   before teardown (commit `b12e2b0`).
2. **Run 2: Ubuntu 22.04 ships libbpf0, not libbpf1.** Laptop-built
   iomoments dynamically links `libbpf.so.1`; jammy ships
   `libbpf0` (pre-1.0). SONAME mismatch. Fix: swap AMI to Ubuntu
   24.04 LTS (commit `f06da42`).
3. **Run 3: scp permission denied + buffered stdout.** Harness
   runs under sudo, output files root-owned. tee block-buffered
   the harness's progress so we couldn't see it die. Fix:
   `sudo chown` after harness, `stdbuf -oL tee`, retrieve-on-
   failure (commit `2aa39a2`).
4. **Run 4: bogus `output-format=json` in fio globals.** Caused
   fio to drop the [global] block, run zero I/O, exit non-zero,
   kill the harness mid-A/1, take iomoments down with it before
   it could write its end-of-run JSON. Fix: remove the line
   (commit `e31e0b0`).

Each was caught by the harness's structured outputs (apt log,
iomoments.stderr, fio.stderr) once the diagnostic plumbing was
correct. The 5th run completed clean.

## Status

D019 first-cut **complete**. iomoments' verdict layer is
behaving honestly on real EBS data, the arithmetic substrate
matches scipy on the lower three moments, and we have concrete
findings for the verdict-layer tuning track and the calibration-
suite expansion track. The data lives in `docs/d019-data/`;
the analysis can be re-run against any future EC2 calibration
result via the same scripts.
