#!/bin/bash
# iomoments pre-commit gate.
#
# Stage 1 (BLOCKING): Python quality gates on staged *.py, clang-format
# check on staged C (no-op today — no C source exists yet per D008).
# Stage 2 (ADVISORY): Gemini independent review on staged code files.
#
# The four C engines (gcc/clang compile-as-lint, clang-tidy, cppcheck,
# scan-build) and the BPF verifier load run at pre-PUSH, not pre-commit
# — see DECISIONS.md D008. This hook intentionally does NOT call them.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

STAGED_PY=$(git diff --cached --name-only --diff-filter=ACM \
              | grep -E '\.py$' || true)
STAGED_C=$(git diff --cached --name-only --diff-filter=ACM \
             | grep -E '\.(c|h|bpf\.c)$' || true)

FAILED=0

# ---------------------------------------------------------------------------
# Stage 1a — Python quality gates (flake8, pylint, mypy --strict, pytest).
# Delegated to the shared tool; the tool no-ops if no .py is staged.
# ---------------------------------------------------------------------------
if [ -n "$STAGED_PY" ]; then
    echo ">>> Python gates"
    if ! "$HOME/tools/code-review/run-python-gates.sh"; then
        FAILED=1
    fi
fi

# ---------------------------------------------------------------------------
# Stage 1b — clang-format check on staged C (formatting only; lint engines
# run at pre-push per D008). .clang-format lands with the first C source.
# ---------------------------------------------------------------------------
if [ -n "$STAGED_C" ]; then
    echo ">>> clang-format"
    if [ ! -f ".clang-format" ]; then
        echo "ERROR: C files staged but .clang-format missing." >&2
        echo "  Add .clang-format alongside the first C source (D008)." >&2
        FAILED=1
    elif ! command -v clang-format >/dev/null 2>&1; then
        echo "ERROR: clang-format not installed." >&2
        FAILED=1
    else
        # shellcheck disable=SC2086  # word-splitting is intentional: one arg per file.
        if ! clang-format --dry-run --Werror $STAGED_C; then
            FAILED=1
        fi
    fi
fi

# Block now if Stage 1 failed — don't burn Gemini budget on broken code.
if [ "$FAILED" -ne 0 ]; then
    echo ""
    echo "QUALITY GATES FAILED — fix before committing." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Stage 2 — Gemini independent review (advisory; never blocks).
# Hangs silently on rate-limit per global CLAUDE.md; invoked with `timeout`.
# Findings print; commit proceeds regardless. The clean-Claude subagent
# review happens in the Claude Code session, not in this hook.
# ---------------------------------------------------------------------------
STAGED_REVIEW=$(git diff --cached --name-only --diff-filter=ACM \
                  | grep -E '\.(py|c|h|bpf\.c|S)$' || true)

if [ -n "$STAGED_REVIEW" ]; then
    echo ""
    echo ">>> Gemini review (advisory)"
    # 120s per the known-hang-mode guidance in global CLAUDE.md.
    # shellcheck disable=SC2086
    timeout 120 "$HOME/tools/code-review/gemini-review.sh" $STAGED_REVIEW \
        || echo "  (Gemini review timed out or errored — advisory, not blocking.)"
fi

echo ""
echo "Pre-commit gates passed."
exit 0
