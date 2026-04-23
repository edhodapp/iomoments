# CD Pipeline Proposals — I/O Moments BPF Tool

> **Note — license section superseded.** §1.3 below recommends BSD-3-Clause.
> That recommendation has been **superseded by `DECISIONS.md` D001**, which
> selects **AGPL-3.0-or-later** at the project level with the BPF source
> file dual-licensed `(GPL-2.0-only OR AGPL-3.0-or-later)` so the kernel's
> `license_is_gpl_compatible()` allowlist accepts it. The §9 summary table
> entry for license is likewise stale. Defer to D001 on anything
> license-related; the rest of this proposal stands.
>
> **Note — C static-analysis stack superseded.** §3 Stage 1's C-gate list
> (`clang-format` / `clang-tidy` / `scan-build` / verifier load / `make
> test`) has been **superseded by `DECISIONS.md` D008**, which re-derives
> for iomoments the four-engine principle from fireasmserver: dual-compiler
> compile-as-lint (gcc + clang) + clang-tidy + cppcheck + scan-build, run
> at **pre-push** rather than pre-commit, with the BPF verifier as the
> ultimate gate and `shellcheck` on `tooling/*.sh`. Pre-commit keeps
> clang-format, Python gates, and Gemini/clean-Claude review. Defer to
> D008 on anything C-lint-related.

Proposal for the deliverable that fills the BPF-moments gap. Drafted after
reading `~/.claude/CLAUDE.md`, `~/PRODUCTS.md`, `~/tools/code-review/*`, and
the sibling-project pipelines in `~/ws_pi5` and `~/fireasmserver`.

Per the "principles transfer; processes do not" rule, nothing from those
projects is copied here — patterns are re-derived against this project's
language mix (C + BPF + Python) and target (Linux kernel tracing).

Everything below is a **proposal for discussion**. Nothing has been
implemented. Decisions flagged as **[DECIDE]** need your input before I
scaffold.

---

## 1. Project Identity

### 1.1 Location — **[DECIDE]**

`~/math/moments/` is the current path, but `~/math/` reads as a playground
directory. Per the CD-first rule, a deliverable belongs in a sibling
directory matching `~/PRODUCTS.md` conventions.

Options:
- **A.** Keep this directory as the math/exploration scratchpad and spawn a
  new repo at e.g. `~/iomoments/` for the deliverable. The markdown notes
  here become design references.
- **B.** Promote `~/math/moments/` into the deliverable repo directly.

**Recommendation: A.** Matches the pattern of every other deliverable in
your tree, keeps exploratory notes out of the shipping repo, and cleanly
invokes the "spawn a new repo, re-derive the pipeline" rule.

### 1.2 Name — **[DECIDE]**

Candidates, all checked unobtrusively for collisions:
- **`iomoments`** — narrow, descriptive, pairs naturally with `biolatency`.
- **`shapesnoop`** — borrows the bcc `-snoop` suffix; emphasizes shape over
  quantile.
- **`pebay-bpf`** — credits the algorithm; niche appeal.
- **`moments-bpf`** / **`bpf-moments`** — generic.

**Recommendation: `iomoments`.** Scope is clear, sits naturally alongside
bcc's `biolatency` in a user's mental model, and doesn't overpromise a
general-purpose moment library.

### 1.3 License — **[DECIDE]**

Three consistent options:
- **A. BSD-3-Clause** — matches `ws_pi5`. Code can flow into your
  closed-source projects (`web_server`, `router`, `5g_modem`, `usphone`) if
  ever useful. Outside contributions permissive.
- **B. AGPL-3.0-or-later** — matches `fireasmserver`, `python_agent`,
  `asm_agent`, `shell_agent`. Strong copyleft; cannot flow into your
  closed-source projects. SaaS-friendly via dual-license.
- **C. Dual BSD-3 / AGPL-3** — complexity, not recommended for a single
  small tool.

**Recommendation: BSD-3-Clause.** This is a diagnostic tool that could
plausibly be useful inside your bare-metal product line (disk/flash
perf characterization on the BeaglePlay, modem-side flow measurement), and
the closed-source projects can't pull AGPL code. BSD keeps doors open. The
Pébay algorithm is already public.

### 1.4 Repo — **[DECIDE]**

`github.com/edhodapp/iomoments` (public). Mirror in `~/PRODUCTS.md` under
Open Source once created.

---

## 2. Language and Toolchain

### 2.1 Language split

- **BPF program** (kernel side): C, compiled by **clang** with
  `-target bpf`. No choice here — this is the BPF ABI.
- **Userspace loader/aggregator**: C using **libbpf**. Consistent with the
  BPF program, smallest footprint, CO-RE support via BTF / `vmlinux.h`.
- **Reference / test oracle**: Python 3.11+ with numpy/scipy. Pébay's
  update rules will be validated numerically against a naïve
  high-precision implementation and against scipy's descriptive-stats
  functions. Runs in the test suite only; not shipped.

**Why not Rust with libbpf-rs?** Fewer dependencies, consistent with
sibling-project toolchains, and no second toolchain to maintain. Rust
would pay off in a larger project; this one stays lean.

### 2.2 Minimum kernel / distro target — **[DECIDE]**

BPF CO-RE with BTF requires kernel ≥ 5.2 for BTF, ≥ 5.4 for CO-RE in
practice. Realistic target: **Linux ≥ 5.15** (Ubuntu 22.04, Debian 12,
RHEL 9). Modern Chromebook-Linux and Pi OS both qualify.

This gates:
- `vmlinux.h` generation via `bpftool btf dump file /sys/kernel/btf/vmlinux`.
- `bpf_printk` and ringbuf availability.
- Per-CPU array maps.

### 2.3 Build deps (runtime of CI + dev)

- `clang` ≥ 14, `llvm` (for `llvm-strip`)
- `libbpf-dev` ≥ 1.0
- `bpftool`
- `linux-headers` matching the running kernel
- `make`, `pkg-config`
- Python 3.11+, venv

All apt-installable on Debian/Ubuntu. Chromebook-Linux can host dev.

---

## 3. Pre-Commit Pipeline (BLOCKING + advisory)

Two-stage, matching the ws_pi5 / fireasmserver pattern but adapted for
C+BPF:

### Stage 1 — Quality gates (BLOCKING)

Runs on every `git commit`. If any sub-gate fails, the commit is blocked.

**C / BPF gates:**
1. **`clang-format --dry-run --Werror`** on staged `*.c`, `*.h`, `*.bpf.c`.
   Style enforcement; we'll commit a `.clang-format` derived from Linux
   kernel style (since BPF C idioms are kernel-adjacent). Not literally
   copied from any project.
2. **`clang-tidy`** with a project `.clang-tidy` config. Equivalent of
   pylint — catches real issues, not style.
3. **`scan-build`** (clang static analyzer) on userspace C. Catches
   use-after-free, null deref, leaks.
4. **BPF verifier dry-run**: `bpftool prog load` against a fresh build of
   the BPF object. If the kernel verifier rejects, commit is blocked.
   This is the C-BPF analogue of mypy — a type/shape check done by the
   kernel.
5. **`make test`** (see §5). Includes Pébay numerical correctness tests.

**Python gates** (only if staged `*.py`):
1. `flake8` — `max-complexity=5`, `max-line-length=79`.
2. `pylint` using `~/.claude/pylintrc`.
3. `mypy --strict`.
4. `pytest --cov --cov-branch` with 100% branch coverage target.

### Stage 2 — Independent reviews (advisory; never blocks)

1. **Gemini review** — invokes `~/tools/code-review/gemini-review.sh` on
   staged C, header, and Python files. That script already supports
   directory mode. We extend its file glob to include `*.c *.h *.bpf.c`
   **in this project's hook**, not globally — per "principles transfer,
   processes do not," the shared script stays general; the project's
   hook filters what to send.
2. **Clean Claude review** — spawn a subagent with no project context
   and the shared review prompt from `~/tools/code-review/review-prompt.txt`
   (which already covers C-adjacent concerns like memory barriers and
   MMIO ordering).

Both reviews run, findings surface, you address them before committing.
Findings from both agreeing → high confidence. Disagreement → judgment.

### Installation

One script, `tooling/hooks/install.sh`, installs:
- `.git/hooks/pre-commit` → symlink to `tooling/hooks/pre-commit.sh`
- Settings for Claude Code `PreToolUse` hooks in `.claude/settings.json`
  (project-scoped, not global)

The project-local `pre-commit.sh` wires Stage 1 (blocking) and invokes
`~/tools/code-review/gemini-review.sh` for Stage 2. **No shared script is
modified** — the project owns its pipeline.

---

## 4. Pre-Push Integration Tests (BLOCKING)

Unit tests prove the software logic; integration tests prove the kernel
actually accepts the BPF and the numbers are right under load.

### Phase 1 — Kernel-in-a-box

Boot a lightweight QEMU VM (or use a Podman container with `--privileged`
and host kernel headers) running a known-good kernel. Load the BPF
program, generate synthetic I/O via `fio` or a simpler loopback file
write, capture moments, compare against a Python reference run on the
same samples recorded via `blktrace`.

This isolates the question "does the tool produce the right numbers on
real kernel I/O?" from hardware variability.

### Phase 2 — Real-hardware perf baseline

On a real disk (the Chromebook's built-in SSD or an external disk —
**NEVER** the SD card, per `~/.claude/CLAUDE.md`), run a fixed `fio`
profile. Record:
- First 6 moments of latency distribution (raw and log-space).
- Memory footprint of the BPF maps.
- Per-sample update overhead (BPF program cycle count via
  `bpf_ktime_get_ns` deltas).
- Comparison HDR histogram for the same workload as sanity check.

Append results to `perf_runs.log` tagged with commit hash. Soft criteria:
a human eyeballs drift; automated thresholds catch gross regressions
only.

### Phase 3 — Merge correctness

Spawn N concurrent load generators, verify that per-CPU partial moments
merge (via Pébay's parallel combine rule) to within floating-point
tolerance of the single-stream result. This is the property test that
makes the kernel-side design trustworthy.

---

## 5. Test Strategy

Behavioral tests first (per your philosophy):
- **B1:** "Given a stream of samples with known mean and variance, the
  tool reports mean and variance within 1 ULP." Drives the basic update.
- **B2:** "Given a log-normal stream, skew and kurtosis in log-space
  match scipy reference within tolerance." Drives the higher-moment
  update.
- **B3:** "Merging two partial summaries equals processing their
  concatenation." Drives per-CPU merge.
- **B4:** "Loading the BPF program on kernel ≥ 5.15 succeeds." Drives
  kernel-side portability.
- **B5:** "Running against a known `fio` workload produces reports that
  correctly rank three distributions by skew." End-to-end shape
  fidelity.

Unit tests fill in branch coverage once code artifacts exist. Target
100% branch coverage for Python; for C, `gcov` / `llvm-cov` over the
userspace portion; BPF program coverage is measured indirectly via the
behavioral tests (verifier acceptance + numerical results).

Requirements checklist tracked in `REQUIREMENTS.md` with columns:
implemented / tested / deviation rationale. Every deviation is either a
defect or a design decision — both logged.

---

## 6. CI / Remote Gates

### 6.1 GitHub Actions matrix — **[DECIDE]**

- **OS matrix:** `ubuntu-22.04`, `ubuntu-24.04`, `debian-12` (via
  container).
- **Kernel matrix:** harder — GitHub runners have fixed kernels. Option:
  use `actions-runner-controller` self-hosted, or test BPF load inside a
  QEMU VM on the runner (slower but portable). Recommendation: **QEMU VM
  with multiple kernel images**, gated on kernel ≥ 5.15.
- **Gates:** Stage 1 (blocking) runs in CI. Stage 2 (reviews) stays
  local — CI doesn't need Gemini/Claude.

### 6.2 Release artifacts

`make release` produces:
- Statically linked userspace binary (x86_64 and aarch64 — matches your
  product line).
- Stripped BPF object.
- SBOM (CycloneDX JSON).
- Signed tag.

---

## 7. Repository Skeleton (proposed, not created)

```
iomoments/
├── .clang-format
├── .clang-tidy
├── .gitignore
├── COPYRIGHT                 # project notice + contribution policy (per global CLAUDE.md split)
├── DECISIONS.md              # immutable ADR log, same style as fireasmserver
├── LICENSE                   # verbatim AGPLv3, unmodified
├── Makefile                  # top-level: all, test, clean, install, release
├── README.md
├── REQUIREMENTS.md           # spec + implemented/tested/deviation table
├── pyproject.toml            # Python tooling only
├── src/
│   ├── iomoments.bpf.c       # BPF program (kernel side)
│   ├── iomoments.c           # userspace loader/aggregator
│   ├── pebay.h               # Pébay update rules (header-only, shared)
│   └── vmlinux.h             # generated; gitignored, rebuild rule in Make
├── tests/
│   ├── test_pebay.c          # C unit tests for Pébay math
│   ├── test_pebay_ref.py     # Python numerical oracle
│   ├── test_load.sh          # BPF load + verifier smoke test
│   └── integration/
│       ├── test_fio.py       # end-to-end against fio workloads
│       └── perf_runs.log     # durable perf history
├── tooling/
│   └── hooks/
│       ├── install.sh
│       ├── pre-commit.sh     # project-local; invokes shared review scripts
│       └── pre-push.sh       # integration tests
└── .github/
    └── workflows/
        └── ci.yml
```

Directory structure is **predicted, not mandated**. Per "structure
emerges" in your philosophy, we commit only what Stage 1 needs to begin,
and grow from there.

---

## 8. What I'd Build First (assuming approval)

1. `git init`, `LICENSE` (verbatim AGPLv3), `COPYRIGHT` (iomoments notice
   per global CLAUDE.md split), `README.md`, `DECISIONS.md` with D001–D00n
   capturing decisions from this doc. *(LICENSE and COPYRIGHT were drafted
   on 2026-04-22 ahead of `git init`; they're sitting in `~/iomoments/`
   waiting to be committed into the first commit.)*
2. `Makefile` with `test`, `clean`, `fmt`, `lint` targets — no functional
   code yet.
3. `.clang-format`, `.clang-tidy`, `pyproject.toml` — quality gate
   configuration.
4. `tooling/hooks/pre-commit.sh` + installer; verify it blocks on a
   deliberately ugly staged change.
5. `tests/test_pebay_ref.py` — Python oracle for moments 1–6, with a
   failing "hello world" test to prove the gate runs.
6. `.github/workflows/ci.yml` — minimal: build + Stage 1 gates on PR.
7. Only then: first line of functional C code.

This matches "CD-first for deliverables" — the pipeline exists and is
proven to block bad code before the first functional commit.

---

## 9. Summary of Decisions Needed from You

| # | Decision | Recommendation |
|---|----------|----------------|
| 1 | Repo location | Spawn `~/iomoments/`, leave `~/math/moments/` as scratchpad |
| 2 | Project name | `iomoments` |
| 3 | License | BSD-3-Clause |
| 4 | GitHub repo | `github.com/edhodapp/iomoments`, public |
| 5 | Minimum kernel | Linux ≥ 5.15 |
| 6 | Language split | C (BPF + userspace libbpf), Python (test oracle only) |
| 7 | CI provider | GitHub Actions with QEMU VM for kernel matrix |
| 8 | Add to `~/PRODUCTS.md` | Yes, under Open Source |

Once you pick or redirect, I'll scaffold step 8.1 (the CD-first skeleton,
no functional code) as a single commit and walk you through verifying
the gates block as expected.
