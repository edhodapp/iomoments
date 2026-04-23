# Architecture Decision Log

Chronological record of design decisions for iomoments. Each entry captures
the decision, the justification, and the date. Entries are numbered
sequentially (D001, D002, ...) and never renumbered.

**Entries are immutable in content.** Once written, the decision text and
rationale are never edited or deleted — the log is a historical record.
Review this log before making new decisions to avoid re-litigating settled
questions.

## Supersession and traceability

When a decision is revised or reversed, **both entries are linked so that
traceability works in either direction** — a reader landing on either entry
can find the other in one step without scanning the whole log.

1. Write a new entry `D00N` with the superseding decision. The first line
   of the body is a **back-pointer**:

   > `**Supersedes:** D003 (deprecated YYYY-MM-DD HH:MM UTC). [brief reason.]`

2. Prepend a **deprecation annotation** to the superseded entry `D003`. The
   annotation is append-only metadata, not a modification of the decision
   text — the original body and rationale stay exactly as written, below
   the annotation:

   > `**DEPRECATED YYYY-MM-DD HH:MM UTC — superseded by D00N.** [brief reason.]`

The annotation is the one exception to content immutability: it records the
*fact of supersession* (a later event), not a revision of the original
decision. The original decision stays intact so the reasoning of the moment
it was made is preserved.

Chained supersessions (`D003 → D00N → D00M`) annotate each link in turn:
`D003` points forward to `D00N`; `D00N` annotation points forward to
`D00M`; `D00M`'s back-pointer names `D00N` (the immediate predecessor) and
may optionally note the earlier ancestor for clarity.

## 2026-04-20

### D001: Platform — Linux eBPF only; AGPL-3.0-or-later with GPL-compat BPF file

Target platform is Linux eBPF only. Minimum kernel 5.15 for CO-RE + BTF.

BSD classic BPF is the original packet-filter design (McCanne & Jacobson,
USENIX '92) but is a fundamentally different system — no kprobes, no maps,
no in-kernel tracing infrastructure comparable to Linux eBPF. FreeBSD has
an experimental eBPF port and DTrace remains the BSD-family equivalent for
this class of observability, but those are separate artifacts, not ports.
The iomoments name refers explicitly to the Linux eBPF tool.

License is AGPL-3.0-or-later at the project level (matches fireasmserver,
python_agent, asm_agent, shell_agent). The BPF object file
(`src/iomoments.bpf.c`) is dual-licensed
`(GPL-2.0-only OR AGPL-3.0-or-later)` via SPDX header, with
`char _license[] SEC("license") = "GPL";` in the source. Rationale: the
kernel's `license_is_gpl_compatible()` allowlist (`kernel/bpf/core.c`)
does not recognize "AGPL" directly, even though AGPL-3.0 is legally
GPL-compatible via AGPL §13. Most tracing helpers iomoments needs
(`bpf_probe_read_kernel`, `bpf_get_stackid`, `bpf_perf_event_read_value`)
are GPL-only; the GPL-2.0-only branch of the dual license is what grants
the BPF program access to them. The AGPL-3.0-or-later branch keeps the
project's overall license identity consistent with the rest of the
open-source product family.

Pébay's update rules (see D006) and the underlying method of moments are
public prior art (Sandia SAND2008-6212; Pearson 1894). AGPL protects
specific code expression only, not algorithms. Sole-author status (see
D002) preserves unrestricted rights to re-implement the same math in
proprietary ARM64 assembly projects without AGPL contamination, provided
those implementations are written from the Pébay paper rather than
translated from the AGPL C code.

### D002: Contributions — single-author, no PRs (SQLite model)

Bug reports welcome via GitHub issues. Pull requests are not accepted.
Fixes described in issues may be reimplemented by the author.

Rationale: preserves sole-author copyright, removes the need for CLAs or
DCOs, and keeps the dual-license play (AGPL public + private commercial
grants to proprietary sibling projects when useful) a memo-to-self
formality rather than a contributor-rights negotiation. Same policy as
fireasmserver (its D001).

### D003: Name — iomoments

Verified clean on 2026-04-20:
- Zero GitHub repositories or code matches on the exact token `iomoments`.
- PyPI unregistered (404 on `https://pypi.org/pypi/iomoments/json`).
- Web search returns only unrelated strings ("imoments", "InMoment" —
  the CX platform company — and Instagram handles); no collision on the
  actual term.

Alternatives considered and rejected:
- `biomoments` — conflates with biological "bio" in web searches and
  "bio" in BPF tooling already denotes block I/O via `biolatency`.
- `shapesnoop` — misleads: implies sketching or quantile recovery, not
  moments specifically.
- `pebay-bpf` — credits the algorithm but obscures purpose; too narrow.
- `bpf-moments` / `moments-bpf` — generic.

`iomoments` reads naturally alongside `biolatency` in a user's mental
model: latency distribution → latency moments.

### D004: Location — dedicated repo at ~/iomoments/

`~/math/moments/` remains the exploratory / notebook directory where the
project question was first worked out. `~/iomoments/` is the clean
CD-first deliverable repo.

Follows the global CLAUDE.md rule: "If an experiment matures into a
deliverable, spawn a new repo that starts CD-first — and per the
'principles transfer; processes do not' rule, re-derive its pipeline from
first principles, don't copy-paste the experimental repo's tooling."

Location convention matches the sibling open-source deliverable projects
in `~/PRODUCTS.md` (ws_pi5, fireasmserver, python_agent, asm_agent,
shell_agent).

### D005: Language split — C for kernel + userspace; Python as test oracle only

- **BPF program** (`src/iomoments.bpf.c`): C, compiled by clang with
  `-target bpf`. Forced by the BPF ABI.
- **Userspace loader / aggregator** (`src/iomoments.c`): C using libbpf.
  Smallest footprint, CO-RE via BTF, consistent with the kernel-side
  code, no second toolchain.
- **Reference / test oracle** (`tests/test_pebay_ref.py`): Python 3.11+
  with numpy/scipy. Validates Pébay updates against a naïve
  high-precision implementation and against scipy's descriptive-stats
  functions. Runs in the test suite only; not shipped.

Rejected:
- **Rust + libbpf-rs**: would pay off in a larger codebase; overhead not
  justified here. Adds a toolchain and a dependency surface for no
  observable benefit at this project's scope.
- **C++**: no need for templates or classes; the project fits plain C,
  and C keeps the kernel-side code uniform with the userspace side.
- **Go**: GC + runtime; incompatible with the bare-metal-assembly
  aesthetic of the sibling projects.

### D006: Algorithm — Pébay 2008 update rules for online moments

Philippe Pébay, *Formulas for Robust, One-Pass Parallel Computation of
Covariances and Arbitrary-Order Statistical Moments*, Sandia National
Laboratories SAND2008-6212. Generalizes Welford (1962) to arbitrary
order and includes a parallel-combine rule (merge two partial summaries
into the summary of their union) — critical for per-CPU accumulation in
BPF maps, which must merge to a single aggregate in userspace.

Properties: numerically stable, branch-free update, integer- / fixed-
point-friendly, mergeable.

Rejected: naïve sum-of-powers accumulation ($\sum x^k$ tracked directly).
Catastrophic cancellation and overflow make higher moments unusable in
realistic sample streams; this is the most common implementation mistake
in the space and must not be reproduced.

Output: moments of $\log(\text{latency})$ in addition to raw moments.
Latency distributions are typically log-normal-ish; moments in log space
converge faster and skew/kurtosis there directly characterize tail shape.
Both representations emitted; downstream consumer selects.

### D007: Core thesis — shape characterization with feasibility verdicts

**This entry is foundational and should be read before D001–D006.** Those
decisions implement the thesis stated here; this entry states the purpose.
Logged after D001–D006 only because the project charter crystallized in
conversation after the tactical choices; the causal priority runs the
other direction.

**Decision.** iomoments exists to provide **compact, reliable shape
characterization** of I/O distributions, not to perform exact density
reconstruction. Its distinguishing feature is a **diagnostic layer** that
emits an explicit feasibility verdict on every run: the tool reports
*when its summary statistics are trustworthy for the observed workload,
and when they are not*. Moments are the chosen primitive for resource
reasons — small, incrementally updatable, mergeable across CPUs — but
the tool refuses to lie about them.

**The two questions.** The classical moment problem (Hamburger 1920,
Stieltjes 1894, Hausdorff 1921; Akhiezer 1965 and Schmüdgen 2017 for
modern references) asks: *given moments $m_k = \int x^k\,d\mu(x)$, can
one recover $\mu$?* In general the answer is "no" — Carleman (1926)
identifies determinate cases, and canonical counterexamples such as the
log-normal distribution are moment-indeterminate (Heyde 1963; Stoyanov
2013). This is the question mathematicians flinch at, rightly.

iomoments poses the engineering question: *given $N$ moments and a
diagnostic battery, can one output a shape summary with a stated validity
domain?* This is well-posed. The answer is yes for workloads that are
well-behaved (tail index above the moment order, moment-determinate,
stationary) and no for those that are not (heavy-tailed, non-stationary,
temporally aliased). iomoments' contribution is to answer *which case
applies for this workload right now*, not to pretend the answer is
always yes.

**The diagnostic layer is load-bearing.** The production build runs:

- A **probe phase** (see future decision) computing:
  determinacy (Carleman partial sum over estimated even moments); Hankel
  matrix conditioning for effective atomic decomposition (Curto & Fialkow
  1991); tail index via the Hill estimator (Hill 1975; Embrechts et al.
  1997); space selection (KS goodness-of-fit to log-normal); half-split
  moment stability giving per-moment noise floors and sample-count
  budgets.
- A **temporal-coherence sidecar** (see future decision) continuously
  monitoring: inter-arrival coefficient of variation; variance of
  windowed moments as a function of window size (plateau = aliasing);
  spectral flatness of windowed-mean streams (Welch 1967); autocorrelation
  structure of the moment time series. Grounded in Shannon (1949) /
  Nyquist (1928) sampling theory.

**Verdict categories are first-class output:**

- **Green** — moments are a trustworthy shape summary for this workload.
  Emit moments with expected error budget.
- **Yellow** — moments are informative but miss some structure (e.g.,
  bimodality). Emit moments with caveats.
- **Amber** — moments are likely biased (e.g., aliasing suspected).
  Emit moments with a diagnostic recommendation.
- **Red** — moments are the wrong primitive (e.g., heavy tail with
  non-existent variance). **Refuse to emit moment-based summary**;
  recommend an alternative tool (DDSketch, HDR Histogram).

**Why this position is correct.** Mathematicians who dismiss moment-based
shape tools typically answer the wrong question: they correctly note
that reconstruction is ill-posed when the user is actually asking
whether the summary is meaningful. Engineers who ship naïve moment
tools answer the right question with wrong honesty: they emit numbers
regardless of whether the workload makes those numbers interpretable.
iomoments occupies the middle: right question, answered with explicit
validity reporting.

**Implications for scope** (binds future decisions):

- Features that strengthen the diagnostic layer are **core**, not
  optional. The diagnostic work is what differentiates iomoments from
  "`biolatency` with extra accumulators."
- Features that emit more numbers without improving validity reporting
  are **out of scope by default**. Adding an eighth moment pays its way
  only if it improves diagnostic resolution.
- Features that remove diagnostic output to "simplify" the tool are
  **rejected**. The verdicts are the product.

**Historical context.** The design has been incubating for approximately
15 years in Ed Hodapp's thinking. Sibling primitives in the streaming /
sketching family — HDR Histogram (Tene), t-digest (Dunning & Ertl 2021),
DDSketch (Masson et al. 2019) — have matured during that window but
answer the *quantile* question, not the *shape-with-validity* question.
Linux eBPF CO-RE (practical ≥ 5.4, mature ≥ 5.15) removed the portability
barriers that previously made in-kernel diagnostic statistics infeasible.

## 2026-04-22

### D008: C static-analysis stack — four independent engines, pre-push

Re-derives for iomoments the principles published by fireasmserver's
four-layer C static-analysis stack. Processes (flag selection, Makefile
wiring, hook layout) are NOT copied — they are re-stated here against
iomoments' actual language mix (eBPF C + libbpf userland C + Python test
oracle + shell tooling).

**Supersedes (partially):** the C-side gate list in
`CD-PIPELINE-PROPOSAL.md` §3 Stage 1, which predates this decision and
enumerates only `clang-format` / `clang-tidy` / `scan-build` / verifier
load / `make test`. That list is incomplete against the principle that
*independent engines catch independent bugs*, and places the heavy
engines at pre-commit rather than pre-push.

**The stack.** Four engines, each run on every C translation unit the
project produces. The engines disagree on edge cases — that disagreement
is the reason to run all four, not a reason to pick one.

1. **Dual-compiler compile-as-lint** — `gcc -c -o /dev/null …` and
   `clang -c -o /dev/null …` against the same flag set. Applies to the
   userland loader (`src/iomoments.c`) and any Python-adjacent C.
   **Does NOT apply to the BPF program**: gcc has no BPF back end, so
   `src/iomoments.bpf.c` is compiled only by clang with `-target bpf`
   and is linted by the BPF-compile path itself.

   Flag set (userland):
   `-Wall -Wextra -Wpedantic -Werror -Wshadow -Wstrict-prototypes`
   `-Wmissing-prototypes -Wdouble-promotion -Wformat=2 -Wcast-align`
   `-Wconversion -Wmissing-field-initializers -std=c11`.
   Additions beyond the fireasmserver reference set:
   - `-Wdouble-promotion` is load-bearing here, not cosmetic: iomoments
     does higher-moment arithmetic where silent `float → double`
     promotion would corrupt the Pébay update invariants (D006).
   - `-Wnull-dereference` (gcc-only) and `-Wthread-safety` (clang-only)
     are added per the kernel-adjacent recommendation. They gate on
     compiler identity in the Makefile.

   Flag set (BPF, clang only): same as userland minus
   `-Wstrict-prototypes` / `-Wmissing-prototypes` (single-unit BPF
   object doesn't have cross-file prototypes to enforce) plus
   `-target bpf -D__TARGET_ARCH_x86` (or `__TARGET_ARCH_arm64`) and
   the CO-RE / vmlinux.h include path. `-Werror` stays.

2. **clang-tidy** — project `.clang-tidy` file at repo root. Baseline
   check set: `bugprone-*`, `cert-*`, `clang-analyzer-*`, `misc-*`,
   `performance-*`, `portability-*`, `readability-*`.
   Disabled with rationale in the `.clang-tidy`:
   - `performance-no-int-to-ptr` — fights BPF map-value pointer idioms.
   - `readability-magic-numbers` on `src/iomoments.bpf.c` only — kernel
     tracepoint offsets are inherently magic.
   - `bugprone-easily-swappable-parameters` globally off — too noisy
     for Pébay's $(n, m_1, m_2, m_3, m_4)$ tuple APIs where all
     arguments are legitimately the same numeric type.
   BPF invocation passes `-- -target bpf -I<vmlinux.h-dir>`. Userland
   invocation uses the compile database produced by `bear`.

3. **cppcheck** — `cppcheck --enable=all --inconclusive --std=c11`
   `--error-exitcode=1 --suppressions-list=tooling/cppcheck.suppress`
   across `src/` and `tests/`. Catches value-agnostic defects
   (uninitialized reads, dead stores, mismatched allocator pairs) that
   clang-analyzer sometimes misses. The suppressions file is
   project-local, every entry carries a one-line rationale, and inline
   `cppcheck-suppress` is preferred over the file where feasible
   (principle 5).

4. **scan-build** — wraps the userland build:
   `scan-build --status-bugs -maxloop 8 make userland`.
   Symbolic execution beyond what clang-tidy's in-process
   `clang-analyzer-*` performs. `--status-bugs` turns any finding into
   a nonzero exit so the gate can block. BPF-target compile is skipped
   from scan-build — the symbolic executor doesn't model BPF-map
   semantics usefully, and the verifier is the correct gate for that
   side (see below).

**The BPF verifier is the ultimate static gate.** Unbounded loops,
unprivileged-helper calls, stack-size overruns, and map-type mismatches
are all verifier-rejected but invisible to the four engines above.
`bpftool prog load` against a fresh build of `iomoments.bpf.o` on a
known-good kernel (≥ 5.15 per D001) is run at pre-push alongside the
four engines. Verifier acceptance is a blocking gate.

**Scheduling — pre-push, not pre-commit.** The full C stack takes
seconds-to-tens-of-seconds on a small project but grows; per-commit
cost compounds. Pre-push matches the integration-test cadence already
established in the global workflow. Pre-commit keeps only the fast
formatting and Python gates:
- `clang-format --dry-run --Werror` on staged C (formatting, not lint).
- `flake8 --max-complexity=5`, `pylint` (Google `pylintrc`),
  `mypy --strict`, `pytest --cov --cov-branch` on staged Python.
- Gemini + clean-Claude review (already in the global workflow).

Pre-push adds:
- Dual-compiler compile-as-lint (userland).
- Clang compile-as-lint (BPF, with `-target bpf`).
- clang-tidy (userland + BPF, different invocations).
- cppcheck (all C).
- scan-build (userland only).
- BPF verifier load.
- `shellcheck` on every `tooling/*.sh`.
- Integration test suite (§4 of the pipeline proposal).

This divergence from the global CLAUDE.md "quality gates are pre-commit"
pattern is deliberate and C-specific: the Python baseline is a single
fast batch, the C baseline is four independent engines plus a kernel
verifier call. The rule's intent (no functional commit escapes
correctness pressure) holds under either scheduling.

**No silent suppressions (principle 5).** Every `// NOLINT`,
`cppcheck-suppress`, or `-Wno-*` override carries an inline comment
with (a) why the rule is wrong for that line and (b) what change to
the surrounding code would make the suppression unnecessary. Project-
level suppression files are allowed for rules whose wrongness is
structural across the whole codebase (e.g., `bugprone-macro-parentheses`
on libbpf CO-RE expansion macros), not as a catch-all.

**Coverage by default (principle 7).** The Makefile's `lint` target
walks `src/**.c` and `tests/**.c` with `find`, not a hardcoded file
list. A new translation unit is picked up automatically. Tests in CI
enforce that every `.c` / `.h` under `src/` and `tests/` was visited
by each of the four engines — a file that was never linted is itself
a gate failure.

**Not re-derived from fireasmserver:** `.clang-tidy` contents,
`tooling/crypto_tests/Makefile` layout, `tooling/hooks/pre_push.sh`.
Those are fireasmserver's *processes*; iomoments will grow its own
equivalents sized for a C+BPF+Python repo whose userland is a single
compilation unit, not a crypto test harness with many drivers.

**Status:** decision logged; implementation happens at repo bootstrap
per the "minimum commit to establish the pipeline" plan in
`CD-PIPELINE-PROPOSAL.md` §8. No code exists yet to lint.

## 2026-04-23

### D009: Ontology DAG as formal verifiable requirements — parallel fork from python_agent

**Decision.** iomoments adopts the **ontology-as-formal-verifiable-
requirements** primitive described in fireasmserver D049: a Pydantic-
typed artifact where each DAG *node* is a complete project-ontology
snapshot (entities, relationships, constraints, module specs, open
questions) and each DAG *edge* carries a `Decision` record (question,
options, chosen, rationale). Git tracks source-level change at file
granularity; the ontology DAG tracks graph-structural change at
constraint granularity plus parallel-design multiplicity (multiple
alternative designs can coexist as sibling branches off one parent).

Per ~/.claude/CLAUDE.md: Ed confirmed 2026-04-23 that `python_agent`
has been **tabled indefinitely**, and iomoments is a parallel
implementation meant to share designs with `fireasmserver` (also
tabled from python_agent's perspective as of 2026-04-19 per its
D049). Lessons crystallizing across the two forks become candidates
for a future standardization effort when it picks up again; neither
fork waits on the other.

**Fork strategy (Option D from the 2026-04-23 proposal).** Baseline
comes from `python_agent/src/python_agent/ontology.py` +
`dag_utils.py` + `types.py` (AGPL-3.0-or-later, compatible with
iomoments' D001 license). Cherry-pick set from fireasmserver:

1. **SysE-grade traceability fields** on `DomainConstraint` —
   `rationale`, `implementation_refs`, `verification_refs`, `status`
   (one of `spec`/`tested`/`implemented`/`deviation`/`n_a`). Phase 2.
2. **`PerformanceConstraint` type** — first-class `metric` / `budget`
   / `unit` / `direction` / `measured_via` fields so measurement-vs-
   budget gaps are a single-tool-reachable concern, not buried in
   description text. Phase 2.
3. **Content-hash idempotent snapshot append** — no-op re-runs don't
   pollute the DAG (compute ontology content hash, compare against
   parent, append only when changed). Phase 3.
4. **Git cross-reference in snapshot labels** — embed current HEAD
   SHA + `+dirty` marker if the working tree has uncommitted changes,
   so any DAG snapshot locates back to the source context in one
   `git show`. Phase 3.
5. **`dag_transaction` context manager with `fcntl.flock`** — load-
   modify-save serialization across concurrent builder processes so
   parallel sessions can't lose each other's updates. Phase 3.
6. **HMAC integrity signing and LLM prompt-injection scan REMOVED**
   — trusted in-repo builder per fireasmserver's same rationale. If
   iomoments ever loads DAGs from a less-trusted source (an agent,
   an external contributor), port the machinery back.

**iomoments-specific extensions** (approved for build-out; draft-
first, crystallize with real friction per fireasmserver D021):

1. **`DiagnosticSignal`** — new type for probe-phase outputs
   (Carleman partial sum, Hankel conditioning, Hill tail-index
   estimator, KS p-value, half-split moment stability). Each carries
   a measurement method + threshold + verdict contribution. Different
   semantics from `PerformanceConstraint` (validity indicator, not
   budget). Phase 4.
2. **`VerdictNode`** — Green/Yellow/Amber/Red vertices with entrance
   criteria expressed in terms of `DiagnosticSignal` thresholds. The
   iomoments signature artifact per D007's load-bearing diagnostic
   layer. Phase 4.
3. **`MomentRepresentation`** — property/attribute capturing whether
   a moment is in raw space or log-space (D006 chose to emit both),
   plus its order k. Either a new top-level type or a constrained
   `Property` — decide in Phase 4 against the first real usage.

**Phased build-out.** Seven commits per the 2026-04-23 task plan:
P1 baseline fork (this commit, D009 entry); P2 SysE extensions; P3
DAG concurrency + content-hash + git-SHA; P4 iomoments-specific
types; P5 initial `iomoments-ontology.json` + builder; P6
`audit-ontology` package; P7 wire audit into pre-push (with its own
D010 entry). Each phase ships with tests and a clean-Claude review
before landing.

**Draft-first discovery (inherited principle from fireasmserver
D021).** The ontology is NOT written waterfall-style before code.
D009's initial ontology (Phase 5) captures what is known *today*
from D001–D008 — platform, algorithm, language split, thesis, lint
discipline. As implementation reveals new constraints
(testability limits, hardware quirks, BPF verifier subtleties,
moment-stability edge cases), they crystallize into the ontology
at the moment of discovery; the audit gate picks them up on the
next commit and verifies them forever after.

**Three-level verification** (D021's adapted DO-178C pattern) is
the aspirational shape once enough real enforcement code exists —
for each `DomainConstraint`: (a) traceability (test named after
the constraint), (b) structural coverage (test actually runs
enforcement code), (c) mutation verification (mutating enforcement
breaks test). Phase 6 `audit-ontology` implements level (a) at
minimum; (b)/(c) are bigger commitments deferred until there's
enough real C/Python enforcement to mutation-test.

**Prior-art scope.** fireasmserver D049 identified Ed's own
`github.com/edhodapp/python-agent` as the base-shape prior art;
iomoments inherits that acknowledgement. iomoments-specific
extensions (DiagnosticSignal, VerdictNode, MomentRepresentation)
are new to this fork; framing them as "novel" against the broader
moment-problem / diagnostic-statistics literature requires review,
not claim.

**Not in this decision's scope** (separate D-entries as they land):
- Audit-tool policy + pre-push wiring — D010, landing with Phase 7.
- Schema extensions beyond the ones named here — added under the
  D009 umbrella without new D-entries.

**Cross-refs:**
- D007 — core thesis; `VerdictNode` is the load-bearing artifact.
- D006 — Pébay algorithm; shape of the moment update is what the
  ontology's constraints and diagnostic signals are about.
- D005 — language split; the ontology lives in Python (tooling side),
  not C, so lint/mypy coverage applies.
- D008 — C lint stack; the ontology's `implementation_refs` can
  point at C symbols, which the Phase 6 audit must resolve via
  ctags or equivalent (design decision for Phase 6).
- fireasmserver D021 / D049 / D051 — the pattern being re-derived.
- python_agent (tabled) — the baseline that was forked.

**Status:** P1 shipped (this commit). P2–P7 tracked in the active
task list; expect ~1 commit per phase.
