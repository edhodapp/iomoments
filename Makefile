# iomoments top-level Makefile.
#
# Python-side gates are stable; C-side gates wired 2026-04-22 per D008.
# BPF-side (bpf-verify target) is stubbed — it becomes a real bpftool
# prog load once the first .bpf.c exists.

VENV            := .venv
VENV_STAMP      := $(VENV)/.installed
PY              := $(VENV)/bin/python
PYTEST          := $(VENV)/bin/pytest
MYPY            := $(VENV)/bin/mypy
PYLINT          := $(VENV)/bin/pylint
FLAKE8          := $(VENV)/bin/flake8
# Repo-committed pylintrc is the sole authority. Matches CI. No fallback
# to $(HOME)/.claude/pylintrc: a silent local pass while CI fails on the
# same commit defeats the purpose of committing the rcfile in the first
# place.
PYLINTRC        := pylintrc

PY_SOURCES      := $(shell find tests tooling -type f -name '*.py' 2>/dev/null)

# ---------------------------------------------------------------------------
# C toolchain. Flag set per D008: dual-compiler compile-as-lint for userland,
# clang-only with -target bpf for BPF objects.
# ---------------------------------------------------------------------------
CC_GCC          := gcc
CC_CLANG        := clang
CLANG_TIDY      := clang-tidy
CLANG_FORMAT    := clang-format
CPPCHECK        := cppcheck
SCAN_BUILD      := scan-build

# Userland flags — shared between gcc and clang compile-as-lint.
# -Wdouble-promotion is load-bearing for Pébay arithmetic (D008 rationale).
CFLAGS_LINT     := -Wall -Wextra -Wpedantic -Werror -Wshadow \
                   -Wstrict-prototypes -Wmissing-prototypes \
                   -Wdouble-promotion -Wformat=2 -Wcast-align \
                   -Wconversion -Wmissing-field-initializers -std=c11

# gcc-only additions.
CFLAGS_LINT_GCC := $(CFLAGS_LINT) -Wnull-dereference

# clang-only additions.
CFLAGS_LINT_CLANG := $(CFLAGS_LINT) -Wthread-safety

# BPF compile flags — clang-only, no cross-unit prototype checks.
# -I/usr/include/x86_64-linux-gnu picks up `asm/types.h` when compiling
# under -target bpf; otherwise Ubuntu's multiarch-split headers aren't
# on the search path.
# -std=gnu11 (not c11) because bpf_helpers.h uses GNU `asm volatile`
# extensions to nudge the verifier on certain helper calls; -std=c11
# rejects those as "undeclared identifier 'asm'".
# -Wno-language-extension-token: libbpf's BPF_PROG macro uses
# `typeof(name(0))` to derive return type; -Wpedantic flags typeof
# as a GNU extension. We accept it for BPF code because the macro is
# the idiomatic fentry/fexit interface.
# -Wno-unused-parameter: BPF_PROG generates a wrapper with an unused
# `ctx` arg that handlers never touch.
CFLAGS_LINT_BPF := -Wall -Wextra -Wpedantic -Werror -Wshadow \
                   -Wdouble-promotion -Wformat=2 -Wcast-align \
                   -Wconversion -Wmissing-field-initializers -std=gnu11 \
                   -Wno-language-extension-token -Wno-unused-parameter \
                   -target bpf -D__TARGET_ARCH_x86 -O2 \
                   -I/usr/include/x86_64-linux-gnu

# Sources collected by walking the tree; no hardcoded file lists.
C_SOURCES       := $(shell find src -maxdepth 2 -type f -name '*.c' \
                     -not -name '*.bpf.c' 2>/dev/null)
C_HEADERS       := $(shell find src -maxdepth 2 -type f -name '*.h' 2>/dev/null)
BPF_SOURCES     := $(shell find src -maxdepth 2 -type f -name '*.bpf.c' 2>/dev/null)
C_TEST_SOURCES  := $(shell find tests/c -maxdepth 2 -type f -name '*.c' 2>/dev/null)
C_ALL           := $(C_SOURCES) $(C_HEADERS) $(BPF_SOURCES) $(C_TEST_SOURCES)

# Monte Carlo tests run separately from the deterministic gate
# (`make test-mc`); excluded from C_TEST_BINS so `make test-c` stays
# fast and false-positive-free.
C_MC_SOURCES    := $(filter tests/c/test_mc.c,$(C_TEST_SOURCES))
C_TEST_FAST_SOURCES := $(filter-out $(C_MC_SOURCES),$(C_TEST_SOURCES))

# Build directory for compiled C test binaries + BPF objects.
BUILD_DIR       := build
C_TEST_BINS     := $(patsubst tests/c/%.c,$(BUILD_DIR)/%,$(C_TEST_FAST_SOURCES))
C_MC_BINS       := $(patsubst tests/c/%.c,$(BUILD_DIR)/%,$(C_MC_SOURCES))
BPF_OBJS        := $(patsubst src/%.c,$(BUILD_DIR)/%.o,$(BPF_SOURCES))
# k=3 fallback variant (D014 / #48): drops m4 update body via
# IOMOMENTS_BPF_K3_ONLY=1, fits stricter verifiers (6.17+) where the
# default k=4 program exceeds the 1M-step budget. Userspace verdict
# layer detects k=3 mode via the `order` parameter on verdict_compute
# and YELLOW's m4-dependent signals.
BPF_K3_OBJS     := $(patsubst src/%.bpf.c,$(BUILD_DIR)/%-k3.bpf.o,$(BPF_SOURCES))

CPPCHECK_SUPPRESS := tooling/cppcheck.suppress

.PHONY: help venv install-hooks test test-c test-mc \
        lint lint-python lint-c lint-c-compile lint-c-tidy lint-c-cppcheck \
        lint-c-scanbuild fmt-check bpf-compile bpf-verify bpf-test-vm \
        bpf-test-vm-matrix iomoments-build \
        build-ontology gate-ontology \
        clean gate-local distclean

# vmtest (D012) reads the guest kernel from here. Ubuntu's shipped
# /boot/vmlinuz-* is modular — vmtest can't boot it without an
# initramfs — so we keep a purpose-built kernel (built via
# ~/vmtest-build/scripts/build_kernel.sh per D012's addendum) at
# a user-readable path.
#
# Single-kernel invocations read KERNEL_IMAGE. Matrix sweeps iterate
# every vmlinuz-v* under ~/kernel-images/ (iomoments floor 5.15
# through a recent LTS).
KERNEL_IMAGE    ?= $(HOME)/kernel-images/vmlinuz-host
KERNEL_MATRIX   := $(wildcard $(HOME)/kernel-images/vmlinuz-v*)

help:
	@echo "iomoments make targets:"
	@echo "  venv           Create .venv and install dev deps."
	@echo "  install-hooks  Symlink project hooks into .git/hooks/."
	@echo "  test           Run C test suite + pytest with branch coverage."
	@echo "  test-c         Compile and run C test binaries only."
	@echo "  lint           Run all lint targets (Python + C)."
	@echo "  lint-python    flake8 + pylint + mypy --strict on tests/."
	@echo "  lint-c         Four-engine C static analysis per D008."
	@echo "  fmt-check      clang-format --dry-run --Werror on all C files."
	@echo "  bpf-compile    clang -target bpf on src/*.bpf.c -> build/*.bpf.o."
	@echo "  bpf-verify     Static bpftool BTF inspection on the .bpf.o (no kernel load)."
	@echo "  bpf-test-vm    Load + run BPF in a VM via vmtest (D012; needs custom kernel)."
	@echo "  bpf-test-vm-matrix  Sweep every ~/kernel-images/vmlinuz-v* kernel."
	@echo "  bpf-overhead   Honest per-event overhead measurement on host kernel (sudo)."
	@echo "  bpf-overhead-vm  Same, inside vmtest under KERNEL_IMAGE (loopback disk)."
	@echo "  iomoments-build Compile the userspace iomoments binary."
	@echo "  build-ontology Rebuild iomoments-ontology.json from the YAML source."
	@echo "  gate-ontology  build-ontology + audit-ontology --exit-nonzero-on-gap (D010)."
	@echo "  gate-local     Full pre-push check: shellcheck + pytest + lints + ontology."
	@echo "  clean          Remove caches."
	@echo "  distclean      clean + remove .venv."

# ---------------------------------------------------------------------------
# Python venv / Python gates.
# ---------------------------------------------------------------------------
$(VENV_STAMP): pyproject.toml
	@if [ ! -d "$(VENV)" ]; then python3 -m venv $(VENV); fi
	$(VENV)/bin/pip install --upgrade pip wheel >/dev/null
	$(VENV)/bin/pip install -e '.[dev]'
	@touch $(VENV_STAMP)

venv: $(VENV_STAMP)

install-hooks:
	tooling/hooks/install.sh

test: $(VENV_STAMP) test-c
	$(PYTEST)

# C test binaries live under $(BUILD_DIR)/. Compiled with the same
# strict flag set used by lint-c-compile; a warning in the test driver
# itself is as much a defect as one in the header it exercises.
$(BUILD_DIR)/%: tests/c/%.c $(C_HEADERS) | $(BUILD_DIR)
	$(CC_CLANG) $(CFLAGS_LINT_CLANG) -o $@ $< -lm

$(BUILD_DIR):
	@mkdir -p $(BUILD_DIR)

test-c: $(C_TEST_BINS)
	@if [ -z "$(C_TEST_BINS)" ]; then \
		echo "(no C tests)"; \
	else \
		for bin in $(C_TEST_BINS); do \
			echo "Running $$bin"; \
			"$$bin" || exit 1; \
		done; \
	fi

# Monte Carlo statistical tests — separate from `test-c` because
# they're probabilistic (band-violation false alarms possible) and
# slow (default 100 trials per fixture). Run periodically rather
# than on every commit. Override trial count via
# `IOMOMENTS_MC_TRIALS=<n> make test-mc`; pin a specific seed via
# `IOMOMENTS_MC_SEED=0x<hex> make test-mc`.
test-mc: $(C_MC_BINS)
	@if [ -z "$(C_MC_BINS)" ]; then \
		echo "(no MC tests)"; \
	else \
		for bin in $(C_MC_BINS); do \
			echo "Running $$bin"; \
			"$$bin" || exit 1; \
		done; \
	fi

lint: lint-python lint-c

lint-python: $(VENV_STAMP)
	@if [ ! -f "$(PYLINTRC)" ]; then \
		echo "ERROR: $(PYLINTRC) missing — refusing to lint without it." >&2; \
		exit 1; \
	fi
	@if [ -z "$(PY_SOURCES)" ]; then \
		echo "(no Python sources to lint.)"; \
		exit 0; \
	fi
	$(FLAKE8) $(PY_SOURCES)
	$(PYLINT) --rcfile=$(PYLINTRC) $(PY_SOURCES)
	$(MYPY) --strict $(PY_SOURCES)

# ---------------------------------------------------------------------------
# C quality gates — four independent engines per D008. Each engine runs
# in its own sub-target so a failure pinpoints which engine disagreed.
# ---------------------------------------------------------------------------
lint-c: lint-c-compile lint-c-tidy lint-c-cppcheck lint-c-scanbuild

# Engine 1: dual-compiler compile-as-lint. gcc and clang disagree on
# edge cases — the disagreement IS the signal.
lint-c-compile:
	@if [ -z "$(C_SOURCES)$(BPF_SOURCES)" ]; then \
		echo "(no C sources to lint.)"; \
	else \
		for f in $(C_SOURCES); do \
			echo "gcc lint   $$f"; \
			$(CC_GCC) -c -o /dev/null $(CFLAGS_LINT_GCC) $$f || exit 1; \
			echo "clang lint $$f"; \
			$(CC_CLANG) -c -o /dev/null $(CFLAGS_LINT_CLANG) $$f || exit 1; \
		done; \
		for f in $(BPF_SOURCES); do \
			echo "clang lint (bpf) $$f"; \
			$(CC_CLANG) -c -o /dev/null $(CFLAGS_LINT_BPF) $$f || exit 1; \
		done; \
	fi

# Engine 2: clang-tidy. Userland and BPF invocations differ; BPF needs
# -target bpf and the CO-RE header path (added as it exists). The
# trailing flags include -Isrc so future headers under src/ resolve
# without a compile_commands.json.
lint-c-tidy:
	@if [ -z "$(C_SOURCES)$(BPF_SOURCES)" ]; then \
		echo "(no C sources for clang-tidy.)"; \
	else \
		for f in $(C_SOURCES); do \
			echo "clang-tidy $$f"; \
			$(CLANG_TIDY) --warnings-as-errors='*' $$f -- -std=c11 -Isrc || exit 1; \
		done; \
		for f in $(BPF_SOURCES); do \
			echo "clang-tidy (bpf) $$f"; \
			$(CLANG_TIDY) --warnings-as-errors='*' $$f \
			  -- -target bpf -std=gnu11 -Isrc \
			  -I/usr/include/x86_64-linux-gnu \
			  -D__TARGET_ARCH_x86 || exit 1; \
		done; \
	fi

# Engine 3: cppcheck. Value-agnostic defects, uninitialized reads,
# mismatched allocator pairs. Globally suppress unmatched-suppression
# and missingIncludeSystem (signal loss accepted; see D008 rationale).
lint-c-cppcheck:
	@if [ -z "$(C_ALL)" ]; then \
		echo "(no C sources for cppcheck.)"; \
	else \
		$(CPPCHECK) --enable=all --inconclusive --std=c11 \
		  --error-exitcode=1 \
		  --inline-suppr \
		  --suppressions-list=$(CPPCHECK_SUPPRESS) \
		  --suppress=missingIncludeSystem \
		  --suppress=unmatchedSuppression \
		  --suppress=checkersReport \
		  -DSEC\(x\)= \
		  -D__always_inline= \
		  $(C_ALL); \
	fi

# Engine 4: scan-build. Path-sensitive symbolic execution beyond what
# clang-tidy's in-process analyzer runs. Userland only — the symbolic
# executor doesn't model BPF-map semantics usefully (D008).
#
# Per-file invocation is a deliberate bootstrap tradeoff: scan-build
# normally wraps a build command and aggregates inter-TU analysis, but
# iomoments has no real build command yet. Switch to
# `scan-build --status-bugs make real-build-target` once the userland
# binary has its own compile step.
lint-c-scanbuild:
	@if [ -z "$(C_SOURCES)" ]; then \
		echo "(no userland C sources for scan-build.)"; \
	else \
		for f in $(C_SOURCES); do \
			echo "scan-build $$f"; \
			$(SCAN_BUILD) --status-bugs -maxloop 8 \
			  $(CC_CLANG) -c -o /dev/null $(CFLAGS_LINT_CLANG) $$f || exit 1; \
		done; \
	fi

# ---------------------------------------------------------------------------
# clang-format — pre-commit gate runs this on staged files; this target
# runs it on every tracked C file for CI and pre-push coverage.
# ---------------------------------------------------------------------------
fmt-check:
	@if [ -z "$(C_ALL)" ]; then \
		echo "(no C files to format-check.)"; \
	else \
		$(CLANG_FORMAT) --dry-run --Werror $(C_ALL); \
	fi

# ---------------------------------------------------------------------------
# BPF compile — clang -target bpf produces build/*.bpf.o from src/*.bpf.c.
# The multiarch include path picks up <asm/types.h> on Ubuntu. The
# ultimate verification (in-kernel verifier load) happens inside a
# vmtest guest via `make bpf-test-vm` per D012; this target only
# produces the object.
# ---------------------------------------------------------------------------
$(BUILD_DIR)/%.bpf.o: src/%.bpf.c | $(BUILD_DIR)
	$(CC_CLANG) $(CFLAGS_LINT_BPF) -g -c $< -o $@

# k=3 variant — same source, IOMOMENTS_BPF_K3_ONLY=1 drops the m4
# update body so the resulting program fits stricter verifier budgets.
$(BUILD_DIR)/%-k3.bpf.o: src/%.bpf.c | $(BUILD_DIR)
	$(CC_CLANG) $(CFLAGS_LINT_BPF) -DIOMOMENTS_BPF_K3_ONLY=1 -g -c $< -o $@

bpf-compile: $(BPF_OBJS) $(BPF_K3_OBJS)

# ---------------------------------------------------------------------------
# Userspace iomoments binary — links libbpf + libelf + libz. Depends on
# the BPF object being compiled (the binary reads build/iomoments.bpf.o
# at load time via libbpf).
# ---------------------------------------------------------------------------
$(BUILD_DIR)/iomoments: src/iomoments.c $(C_HEADERS) $(BPF_OBJS) | $(BUILD_DIR)
	$(CC_CLANG) $(CFLAGS_LINT_CLANG) -o $@ $< -lbpf -lelf -lz -lm

iomoments-build: $(BUILD_DIR)/iomoments

# ---------------------------------------------------------------------------
# bpf-verify — static BTF inspection via bpftool. Does NOT load into the
# running kernel (D012 forbids host-side loads). In-kernel verification
# happens inside vmtest via bpf-test-vm once a vmtest-ready kernel exists.
# ---------------------------------------------------------------------------
bpf-verify: $(BPF_OBJS)
	@if [ -z "$(BPF_OBJS)" ]; then \
		echo "(no BPF sources; bpf-verify is a no-op today.)"; \
	else \
		bpftool_bin=$$(ls /usr/lib/linux-tools/*/bpftool 2>/dev/null | \
			sort -V | tail -1); \
		if [ -z "$$bpftool_bin" ] || [ ! -x "$$bpftool_bin" ]; then \
			echo "WARN: no versioned bpftool found under" \
				"/usr/lib/linux-tools/*/; BTF dump skipped." >&2; \
			echo "  The clang -target bpf compile succeeded, which is" >&2; \
			echo "  the primary gate. Install linux-tools-<kernel> for" >&2; \
			echo "  a full BTF inspection." >&2; \
			echo "bpf-verify (compile-only) clean."; \
		else \
			for obj in $(BPF_OBJS); do \
				echo "bpftool btf dump $$obj"; \
				"$$bpftool_bin" btf dump file $$obj > /dev/null \
					|| exit 1; \
			done; \
			echo "bpf-verify (static BTF dump) clean."; \
		fi; \
	fi

# ---------------------------------------------------------------------------
# VM-side BPF tests (D012). Runs the BPF program inside a vmtest guest so a
# verifier slip, a hung tracepoint, or a verifier-bug exploit can't affect
# the host kernel. Stubbed until src/iomoments.bpf.c exists; when it does,
# the fail-clearly branch runs vmtest pointing at $(KERNEL_IMAGE).
# ---------------------------------------------------------------------------
bpf-test-vm: $(BPF_OBJS)
	@if [ -z "$(BPF_OBJS)" ]; then \
		echo "(no BPF sources; bpf-test-vm is a no-op today.)"; \
	elif [ ! -f "$(KERNEL_IMAGE)" ]; then \
		echo "ERROR: vmtest-ready kernel not found at $(KERNEL_IMAGE)." >&2; \
		echo "  The host's /boot kernel is modular and won't mount 9p" >&2; \
		echo "  root in the vmtest guest. Build a vmtest-ready kernel via" >&2; \
		echo "  ~/vmtest-build/scripts/build_kernel.sh v<version> and copy" >&2; \
		echo "  bzImage-v<version>-default to $(KERNEL_IMAGE), or override" >&2; \
		echo "  via KERNEL_IMAGE=/path/to/vmlinuz." >&2; \
		exit 1; \
	elif ! command -v vmtest >/dev/null 2>&1 && [ ! -x "$(HOME)/.cargo/bin/vmtest" ]; then \
		echo "ERROR: vmtest not on PATH." >&2; \
		echo "  Install: cargo install vmtest" >&2; \
		exit 1; \
	else \
		vmtest_bin=$$(command -v vmtest || echo "$(HOME)/.cargo/bin/vmtest"); \
		bpftool_bin=$$(ls /usr/lib/linux-tools/*/bpftool 2>/dev/null | \
			sort -V | tail -1); \
		if [ -z "$$bpftool_bin" ] || [ ! -x "$$bpftool_bin" ]; then \
			bpftool_bin=$$(command -v bpftool); \
		fi; \
		if [ -z "$$bpftool_bin" ]; then \
			echo "ERROR: no usable bpftool binary found." >&2; \
			echo "  Install: sudo apt install linux-tools-generic" >&2; \
			exit 1; \
		fi; \
		for obj in $(BPF_OBJS); do \
			pin=/sys/fs/bpf/iomoments_$$(basename $$obj .bpf.o); \
			echo "vmtest load $$obj against $(KERNEL_IMAGE)"; \
			"$$vmtest_bin" --kernel "$(KERNEL_IMAGE)" -- \
				"$$bpftool_bin" prog load "$$obj" "$$pin" \
				|| exit 1; \
		done; \
	fi

# Sweep every vmtest-ready kernel under ~/kernel-images/vmlinuz-v*
# against the BPF objects. Catches kernel-version sensitivity at the
# verifier-load layer — iomoments' floor (5.15 per D001) and recent
# LTS lines must all accept the program.
bpf-test-vm-matrix: $(BPF_OBJS)
	@if [ -z "$(BPF_OBJS)" ]; then \
		echo "(no BPF sources; bpf-test-vm-matrix is a no-op today.)"; \
	elif [ -z "$(KERNEL_MATRIX)" ]; then \
		echo "ERROR: no kernels in ~/kernel-images/vmlinuz-v*." >&2; \
		echo "  Build via ~/vmtest-build/scripts/build_kernel.sh v<ver> default" >&2; \
		echo "  then: cp ~/vmtest-build/bzImage-v<ver>-default ~/kernel-images/vmlinuz-v<ver>" >&2; \
		exit 1; \
	else \
		for k in $(KERNEL_MATRIX); do \
			echo ""; \
			echo "=== kernel: $$k ==="; \
			$(MAKE) bpf-test-vm KERNEL_IMAGE=$$k || exit 1; \
		done; \
	fi

# ---------------------------------------------------------------------------
# Honest per-event overhead measurement. Loads the BPF program on the
# host kernel (real I/O traffic), enables kernel.bpf_stats_enabled,
# generates direct-I/O load via dd, reads run_time_ns / run_cnt from
# bpftool, computes per-event ns. Tries k=4 default first; falls back
# to k=3 on verifier rejection (mirrors the runtime selection in
# src/iomoments.c). Requires sudo.
# ---------------------------------------------------------------------------
bpf-overhead: $(BPF_OBJS) $(BPF_K3_OBJS)
	@if [ -z "$(BPF_OBJS)" ]; then \
		echo "(no BPF sources; bpf-overhead is a no-op today.)"; \
	else \
		echo "Running scripts/measure_bpf_overhead.sh on host kernel."; \
		echo "Requires sudo for BPF attach + sysctl + direct I/O."; \
		sudo scripts/measure_bpf_overhead.sh \
			$(BPF_OBJS) $(BPF_K3_OBJS); \
	fi

# Same measurement but inside a vmtest guest with a loopback disk —
# lets us exercise kernels other than the host's. Particularly
# useful for the k=4 variant which the host (6.17) rejects but
# every kernel in the supported range (5.15-6.12) accepts.
# Override KERNEL_IMAGE to pick which kernel to measure under.
bpf-overhead-vm: $(BPF_OBJS) $(BPF_K3_OBJS)
	@if [ -z "$(BPF_OBJS)" ]; then \
		echo "(no BPF sources; bpf-overhead-vm is a no-op today.)"; \
	elif [ ! -f "$(KERNEL_IMAGE)" ]; then \
		echo "ERROR: KERNEL_IMAGE not found at $(KERNEL_IMAGE)." >&2; \
		exit 1; \
	else \
		scripts/measure_bpf_overhead_in_vm.sh "$(KERNEL_IMAGE)"; \
	fi

# ---------------------------------------------------------------------------
# Ontology gate (D010). build-ontology is idempotent — no-op if the YAML
# content hash matches the DAG's current node. gate-ontology rebuilds
# first so the audit always reads an up-to-date DAG (catches "edited the
# YAML, forgot to rebuild" drift), then audits with --exit-nonzero-on-gap.
# ---------------------------------------------------------------------------
build-ontology: $(VENV_STAMP)
	$(VENV)/bin/build-iomoments-ontology

gate-ontology: build-ontology
	$(VENV)/bin/audit-ontology --exit-nonzero-on-gap

# ---------------------------------------------------------------------------
# Meta.
# ---------------------------------------------------------------------------
gate-local: lint test gate-ontology
	@if command -v shellcheck >/dev/null 2>&1; then \
		find tooling scripts -type f -name '*.sh' -print0 \
		  | xargs -0 -r shellcheck; \
	else \
		echo "SKIP: shellcheck not installed."; \
	fi

clean:
	rm -rf .mypy_cache .pytest_cache .coverage $(BUILD_DIR)
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV)
