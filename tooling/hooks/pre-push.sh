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
# Ontology gate — rebuild the DAG from YAML (idempotent; no-op if content
# unchanged), then audit with --exit-nonzero-on-gap. Per D010: level-1
# traceability (refs point at real files/symbols) + status/refs consistency.
#
# Ordering invariant: this block runs AFTER pytest. If a pytest regression
# breaks the audit tool itself, pytest fails first and the ontology gate
# never runs — ontology drift cannot be masked by a broken verdict. Don't
# reorder without preserving that invariant.
# ---------------------------------------------------------------------------
echo ""
echo ">>> ontology gate (D010)"
if [ -d ".venv" ] && [ -f ".venv/bin/audit-ontology" ]; then
    if ! make gate-ontology; then
        FAILED=1
    fi
else
    echo "ERROR: no .venv/bin/audit-ontology — run 'make venv' first." >&2
    FAILED=1
fi

# ---------------------------------------------------------------------------
# C-side gates — four independent engines plus clang-format, all driven
# through the Makefile so local and CI run identical invocations. Each
# target no-ops gracefully when its inputs don't exist (e.g., fmt-check
# with zero C files), so the guards here are mostly documentation.
# ---------------------------------------------------------------------------
if [ -d src ]; then
    echo ""
    echo ">>> clang-format (whole tree)"
    if ! make fmt-check; then
        FAILED=1
    fi

    echo ""
    echo ">>> C static analysis (four engines)"
    if ! make lint-c; then
        FAILED=1
    fi

    echo ""
    echo ">>> BPF verifier load"
    # Stub today; real bpftool prog load lands with first iomoments.bpf.c.
    # The Makefile target fails hard if BPF sources exist but the wiring
    # is incomplete, which is the tripwire we want.
    if ! make bpf-verify; then
        FAILED=1
    fi
fi

if [ "$FAILED" -ne 0 ]; then
    echo ""
    echo "PRE-PUSH GATES FAILED — fix before pushing." >&2
    exit 1
fi

echo ""
echo "Pre-push gates passed."
exit 0
