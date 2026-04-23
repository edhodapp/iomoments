#!/bin/bash
# iomoments pre-push gate.
#
# Runs the heavyweight checks that don't belong at per-commit cadence:
# - shellcheck on all tooling shell scripts.
# - Full Python test suite (pytest) — catches regressions the staged-file
#   filter at pre-commit can miss.
# - [DEFERRED — lands with first C source per D008:] four-engine C static
#   analysis (gcc + clang compile-as-lint, clang-tidy, cppcheck, scan-build)
#   and BPF verifier load (bpftool prog load on iomoments.bpf.o).
#
# Rationale for pre-push over pre-commit: D008. Per-commit cost compounds
# when independent engines run on every commit; pre-push matches the
# integration-test cadence established in the global workflow.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

FAILED=0

# ---------------------------------------------------------------------------
# Static-check every tooling shell script (shellcheck). Principle 7 (D008):
# coverage by default — walk the tree, don't hardcode a list.
# ---------------------------------------------------------------------------
echo ">>> shellcheck"
if ! command -v shellcheck >/dev/null 2>&1; then
    echo "ERROR: shellcheck not installed." >&2
    FAILED=1
else
    mapfile -t SHELL_SCRIPTS < <(find tooling -type f -name '*.sh' 2>/dev/null)
    if [ "${#SHELL_SCRIPTS[@]}" -gt 0 ]; then
        if ! shellcheck "${SHELL_SCRIPTS[@]}"; then
            FAILED=1
        fi
    else
        echo "  (no shell scripts under tooling/ to check.)"
    fi
fi

# ---------------------------------------------------------------------------
# Full Python test suite — branch coverage on the oracle. pyproject.toml
# configures --cov --cov-branch; we just invoke pytest.
# ---------------------------------------------------------------------------
echo ""
echo ">>> pytest (full suite)"
if [ -d ".venv" ] && [ -f ".venv/bin/pytest" ]; then
    if ! .venv/bin/pytest; then
        FAILED=1
    fi
else
    echo "ERROR: no .venv/bin/pytest — run 'make venv' first." >&2
    FAILED=1
fi

# ---------------------------------------------------------------------------
# DEFERRED: C-side engines and BPF verifier load. Per D008, these wire in
# the same commit as the first C translation unit. When that commit lands,
# this section fills in with:
#
#   make lint-c       # dispatches to gcc/clang lint, clang-tidy, cppcheck, scan-build
#   make bpf-verify   # bpftool prog load against iomoments.bpf.o
#
# Until then, the comment itself is the pointer for the next author
# (probably future-me) so nothing is forgotten.
# ---------------------------------------------------------------------------
C_SOURCES=$(find src -type f \( -name '*.c' -o -name '*.h' \) 2>/dev/null || true)
if [ -n "$C_SOURCES" ]; then
    echo ""
    echo "WARN: C source present but C-engine pre-push wiring is still deferred." >&2
    echo "  Implement per DECISIONS.md D008 before this push lands on main." >&2
    FAILED=1
fi

if [ "$FAILED" -ne 0 ]; then
    echo ""
    echo "PRE-PUSH GATES FAILED — fix before pushing." >&2
    exit 1
fi

echo ""
echo "Pre-push gates passed."
exit 0
