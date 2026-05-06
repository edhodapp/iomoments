# D019 calibration harness

System-layer calibration for iomoments. Runs three fio workload
classes against a real block device, captures iomoments output +
ground-truth raw latency samples, lets offline analysis verify
that iomoments' verdict matches the workload's actual shape.

See `DECISIONS.md` D019 (queued) for the design rationale; this
directory holds the fio job files + the harness that drives them.
The math-layer counterpart (D018) lives in
`tests/test_calibration_moments.py`.

## What runs

| Class | Workload                              | Target verdict |
|------:|---------------------------------------|----------------|
| A     | Random 4K reads at 2K IOPS, QD=4      | GREEN          |
| B     | Random 4K reads at 12K IOPS, QD=128   | AMBER / RED    |
| C     | Random reads, bsrange 4k–256k, QD=8   | YELLOW         |

A is well-behaved (under gp3's 3K baseline; no throttling). B
oversubscribes the IOPS budget by 4× to provoke EBS throttling
and Pareto-tail shape. C uses a 64× block-size range to construct
a bimodal distribution by mixing IOPS-bound 4K reads with
bandwidth-bound 256K reads.

Each class runs `REPS` reps (default 3) for within-class variance.

## How to run

### EC2 (recommended): one-shot provisioning + teardown

`provision.sh` spins up a fresh `m5.large` (Ubuntu 22.04 LTS HWE,
kernel 6.8) with a separate 50 GB gp3 EBS data volume, copies the
locally-built iomoments binary + harness, runs all classes × reps,
retrieves the output tree, and tears down everything (instance,
volumes, SG, key pair). Single command from the repo root:

```
make iomoments-build       # ensure local binaries + BPF objects
./scripts/calibration_d019/provision.sh
```

Cost ≈ $0.20 per run. Results land at
`scripts/calibration_d019/out-ec2/<run_id>/`.

### Local (manual): on a laptop or pre-provisioned host

Requires root (CAP_BPF + CAP_PERFMON) and a raw block device:

```
sudo IOMOMENTS_DEVICE=/dev/nvme1n1 ./run.sh
```

Optional env vars: `IOMOMENTS_BIN`, `FIO_BIN`, `OUT_DIR`, `REPS`,
`CLASSES`, `IOMOMENTS_DURATION`. See `run.sh` header for details.

## Output layout

```
out/<class>/<rep>/
├── iomoments.json    # iomoments --json output (verdict, moments, signals)
├── iomoments.stderr  # iomoments warmup + ringbuf stats
├── raw.bin           # sequence of uint64 little-endian latency_ns samples
├── fio.json          # fio's structured report
├── fio.stdout        # fio human-readable summary
├── fio.stderr        # fio diagnostics
└── meta.txt          # kernel, fio version, distro, EC2 metadata, timing
```

The `raw.bin` format is one `uint64_t` little-endian per sample, no
header. Read it with:

```python
import numpy as np
xs = np.fromfile("out/A/1/raw.bin", dtype="<u8")
```

## Calibration claim

For each class, the calibration question is: *does iomoments'
verdict match the workload's actual distributional shape*?

Ground truth comes from the raw sample dump:

  - `scipy.stats` on `xs` for the moments (mean, var, skew, kurt).
  - Hill α from the top-K largest samples (independent
    estimator, same paper as iomoments' BPF-side reservoir).
  - KS goodness-of-fit against log-normal / Pareto / bimodal
    fits.

iomoments' verdict and moments come from `iomoments.json`. A
calibration finding looks like one of:

  - **Verdict matches expectation, moments within tolerance**:
    iomoments classified the workload correctly.
  - **Verdict matches, moments outside tolerance**: BPF-side
    fixed-point arithmetic or sampling drift; investigate.
  - **Verdict mismatch, scipy supports iomoments**: the
    verdict-layer threshold band is miscalibrated; tune in a
    follow-on D-entry.
  - **Verdict mismatch, scipy supports the expected verdict**:
    iomoments missed the call. Threshold-band tuning needed; this
    is the load-bearing finding for D019.
