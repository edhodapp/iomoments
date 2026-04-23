#!/bin/bash
# Install iomoments git hooks.
#
# Idempotent: re-running is safe and will overwrite existing symlinks to
# point at the current tooling/hooks/ targets. Real hooks that predate
# this tool (not symlinks) are preserved — the script refuses to clobber
# them and asks for manual resolution.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

HOOKS_DIR="$REPO_ROOT/.git/hooks"
TOOL_DIR="$REPO_ROOT/tooling/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
    echo "ERROR: $HOOKS_DIR does not exist. Run from a git repo root." >&2
    exit 1
fi

install_hook() {
    local name="$1"
    local src="$TOOL_DIR/${name}.sh"
    local dst="$HOOKS_DIR/$name"

    if [ ! -x "$src" ]; then
        echo "ERROR: $src missing or not executable." >&2
        return 1
    fi

    if [ -e "$dst" ] && [ ! -L "$dst" ]; then
        echo "ERROR: $dst exists and is not a symlink." >&2
        echo "  Refusing to overwrite a real hook file. Move it aside first." >&2
        return 1
    fi

    ln -sf "$src" "$dst"
    echo "Installed: $dst -> $src"
}

install_hook "pre-commit"
install_hook "pre-push"

echo ""
echo "Hooks installed. Verify:"
echo "  ls -l $HOOKS_DIR/pre-commit $HOOKS_DIR/pre-push"
