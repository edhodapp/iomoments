# AWS BPF tracer findings — #46 (faithfulness probe)

**Initial probe:** 2026-04-28. **Floor witness added:** 2026-04-29 (D016).
**Probe script:** `scripts/aws_tracer.sh`
**Question (D014 #46):** Do AWS-hosted distro kernels produce the same BPF-verifier verdicts that local `vmtest` predicts?

## TL;DR

vmtest is faithful within its modeled range. The k=4 → k=3 boundary
predicted by vmtest reproduces in cloud, with the same root-cause
rejection signature (verifier 1M-step budget exhaustion). The 5.15
floor (D001) is now exercised in cloud via Ubuntu 20.04, which ships
`5.15.0-1084-aws` by default — both BPF variants verifier-accept.

The first probe surfaced one open side finding still worth tracking
before #47:

1. Amazon Linux 2023 ships kernel **6.18** — one minor above the top
   of our local vmtest matrix (v6.17). The matrix needs to extend.

## Method

- Probe: `t3.small` in `us-east-1`, latest official AMI for each
  distro, ssh in, `dnf`/`apt-get install bpftool`, scp both
  `iomoments.bpf.o` (k=4) and `iomoments-k3.bpf.o` (k=3), run
  `sudo bpftool prog load <obj> <pin>`, capture exit code +
  stderr (verifier log on rejection). Hardcoded teardown via
  `trap teardown EXIT`.
- Faithfulness bar: verdict (accept/reject) + rejection-reason
  match against the closest vmtest kernel from D014's matrix.
- Comparison side: D014-recorded vmtest results from
  `make bpf-test-vm-matrix` against `~/kernel-images/vmlinuz-vX.Y`
  for X.Y ∈ {5.15, 6.1, 6.6, 6.12, 6.17}.

## Observed

| AMI source | AMI ID | Kernel | k=4 verdict | k=3 verdict |
|---|---|---|---|---|
| Canonical `ubuntu-focal-20.04-amd64-server-*` | `ami-0fb0b230890ccd1e6` | `5.15.0-1084-aws` | accept | accept |
| Canonical `ubuntu-jammy-22.04-amd64-server-*` | `ami-0ff290337e78c83bf` | `6.8.0-1052-aws` | accept | accept |
| Amazon `al2023-ami-2023.*-x86_64` | `ami-0c1e21d82fe9c9336` | `6.18.20-20.229.amzn2023.x86_64` | **reject** | accept |

AL2023 k=4 rejection root cause (verbatim, from
`build/aws-tracer/al2023/k4.log`):

```
processed 1000001 insns (limit 1000000) max_states_per_insn 73
  total_states 49038 peak_states 3424 mark_read 0
libbpf: prog 'iomoments_rq_complete': failed to load: -7
```

Same 1M-step verifier-budget overflow that
`feedback_bpf_verifier_complexity_6_12.md` recorded for k=4 against
upstream 6.12, and that D014 §1 records for vmtest v6.17. The
multi-precision k=4 path-explosion bug reproduces on AL2023's 6.18.

## Faithfulness comparison vs vmtest (D014 matrix)

| AWS kernel | Closest vmtest | vmtest k=4 | AWS k=4 | vmtest k=3 | AWS k=3 |
|---|---|---|---|---|---|
| Ubuntu 20.04 / 5.15 | exact match v5.15 | accept | **accept** ✓ | accept | **accept** ✓ |
| Ubuntu 22.04 / 6.8 | bracketed by v6.6 (accept) and v6.12 (accept) | accept | **accept** ✓ | accept | **accept** ✓ |
| AL2023 / 6.18 | above top — extrapolate from v6.17 | reject | **reject** ✓ | accept | **accept** ✓ |

Verdicts match. Rejection signature for AL2023 matches: vmtest v6.17
fails k=4 on the same 1M-step budget overflow with the same
multi-precision-arithmetic path-explosion shape. Ubuntu 20.04 is the
**only exact-version match** in the table — Canonical's `linux-aws`
flavor on focal still tracks 5.15 (kernel rev `5.15.0-1084-aws`),
making this row a same-version comparison rather than a cluster
estimate.

## Caveats

- Two of three AWS kernels (Ubuntu 22.04, AL2023) are not exact-
  version matches against any vmtest kernel — bracket / extrapolation
  comparisons. Ubuntu 20.04 (5.15) is the one same-version match.
- vmtest configures the kernel with the `fedora38` config. Cloud
  vendor kernels carry their own configs and patch sets. Same-cluster
  agreement here suggests config differences are not changing the
  k=4 boundary's location, but a single probe cannot prove that
  generally.
- Canonical may roll the focal `-aws` HWE kernel forward at any
  point; today's exact-5.15 witness becomes a 6.x witness if/when
  that happens. Re-running the probe periodically catches it.
- We did not capture insn-count for the accepting cases. `bpftool
  prog load` (without `-d`) prints nothing on success. If we want
  insn-count drift detection in #47, the AWS-side loader needs `-d`
  or libbpf with explicit `log_level=2` (then a side-channel for
  reading the log on success).
- Single probe, single AMI per distro, single point in time. AWS AMI
  contents change weekly; a re-run in 6 months may land on a
  different kernel. #47's matrix orchestrator should record AMI ID +
  uname -r alongside verdicts so historical comparisons stay
  meaningful.

## Implications for #47 (cloud matrix orchestrator)

1. **vmtest matrix needs to extend up.** AL2023 is already on 6.18.
   Add v6.18 (and likely v6.19+ as they release) to the local
   matrix so we have an apples-to-apples comparator instead of
   extrapolating.
2. **5.15-floor cloud coverage is now established.** Resolved by
   D016 — Canonical Ubuntu 20.04 ships 5.15.0-1084-aws and is the
   chosen 5.15 witness. The earlier "no AWS data point on the
   floor" gap is closed.
3. **The k=4-to-k=3 fallback boundary is the right architecture.**
   It is what made AL2023 a clean degradation rather than an
   outright load failure on this probe. D011/D014's design
   choice holds up under real cloud kernels.
4. **AWS is a faithful test bed for #47** within the limits above.
   No surprise vendor-specific verifier behavior was observed; the
   rejection on AL2023 is the same rejection vmtest already
   predicts at the relevant kernel cluster, for the same reason.

## Reproduction

```sh
# Local: ensure ~/.aws/credentials has profile [iomoments] with
# the iomoments-tracer IAM user's access key, region us-east-1.
make bpf-compile     # produces build/iomoments.bpf.o + iomoments-k3.bpf.o
bash scripts/aws_tracer.sh
# Results land in build/aws-tracer/<distro>/{meta.txt, k4.log,
#  k3.log, k4.verdict, k3.verdict, install.txt, bpftool_path.txt}
# Teardown is automatic via trap EXIT, including a final inventory
# check that prints a manual cleanup command if anything survived.
```
