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

PY_SOURCES      := $(shell find tests -type f -name '*.py' 2>/dev/null)

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
CFLAGS_LINT_BPF := -Wall -Wextra -Wpedantic -Werror -Wshadow \
                   -Wdouble-promotion -Wformat=2 -Wcast-align \
                   -Wconversion -Wmissing-field-initializers -std=c11 \
                   -target bpf -D__TARGET_ARCH_x86 -O2

# Sources collected by walking the tree; no hardcoded file lists.
C_SOURCES       := $(shell find src -maxdepth 2 -type f -name '*.c' \
                     -not -name '*.bpf.c' 2>/dev/null)
C_HEADERS       := $(shell find src -maxdepth 2 -type f -name '*.h' 2>/dev/null)
BPF_SOURCES     := $(shell find src -maxdepth 2 -type f -name '*.bpf.c' 2>/dev/null)
C_ALL           := $(C_SOURCES) $(C_HEADERS) $(BPF_SOURCES)

CPPCHECK_SUPPRESS := tooling/cppcheck.suppress

.PHONY: help venv install-hooks test \
        lint lint-python lint-c lint-c-compile lint-c-tidy lint-c-cppcheck \
        lint-c-scanbuild fmt-check bpf-verify clean gate-local distclean

help:
	@echo "iomoments make targets:"
	@echo "  venv           Create .venv and install dev deps."
	@echo "  install-hooks  Symlink project hooks into .git/hooks/."
	@echo "  test           Run pytest with branch coverage."
	@echo "  lint           Run all lint targets (Python + C)."
	@echo "  lint-python    flake8 + pylint + mypy --strict on tests/."
	@echo "  lint-c         Four-engine C static analysis per D008."
	@echo "  fmt-check      clang-format --dry-run --Werror on all C files."
	@echo "  bpf-verify     bpftool prog load on .bpf.o (stub until first .bpf.c)."
	@echo "  gate-local     Full pre-push check: shellcheck + pytest + all lints."
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

test: $(VENV_STAMP)
	$(PYTEST)

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
			  -- -target bpf -std=c11 -Isrc -D__TARGET_ARCH_x86 || exit 1; \
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
		  --suppressions-list=$(CPPCHECK_SUPPRESS) \
		  --suppress=missingIncludeSystem \
		  --suppress=unmatchedSuppression \
		  --suppress=checkersReport \
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
# BPF verifier load — the ultimate static gate (D008). No .bpf.c exists
# yet, so this is a stub that fires only if BPF sources land. Real wiring
# grows here once src/iomoments.bpf.c is written.
# ---------------------------------------------------------------------------
bpf-verify:
	@if [ -z "$(BPF_SOURCES)" ]; then \
		echo "(no BPF sources; bpf-verify is a no-op today.)"; \
	else \
		echo "ERROR: BPF sources present but bpf-verify is not yet implemented." >&2; \
		echo "  Wire: clang -target bpf -> iomoments.bpf.o -> bpftool prog load." >&2; \
		exit 1; \
	fi

# ---------------------------------------------------------------------------
# Meta.
# ---------------------------------------------------------------------------
gate-local: lint test
	@if command -v shellcheck >/dev/null 2>&1; then \
		find tooling -type f -name '*.sh' -print0 \
		  | xargs -0 -r shellcheck; \
	else \
		echo "SKIP: shellcheck not installed."; \
	fi

clean:
	rm -rf .mypy_cache .pytest_cache .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV)
