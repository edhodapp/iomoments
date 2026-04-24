"""Static BPF smoke tests.

Shells out to `make bpf-verify` (which compiles the BPF objects and
runs `bpftool btf dump` on each). Caught regressions:
- BPF compile failure (e.g., missing libbpf-dev, asm/types.h not on
  include path, clang -target bpf flag changes).
- BTF malformed output (bpftool btf dump exits nonzero).

In-kernel verification and functional tests live in `make bpf-test-vm`
which runs inside a vmtest guest (D012); this file stays host-side
and never loads a program.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# rglob so BPF sources organized into subdirs under src/ (e.g.,
# src/probes/foo.bpf.c) are still picked up.
_BPF_SOURCES = list((_REPO_ROOT / "src").rglob("*.bpf.c"))


def test_bpf_verify_passes() -> None:
    """make bpf-verify returns 0 — BPF compiles and BTF dumps cleanly.

    bpf-verify's dependency graph runs bpf-compile first via make's
    own pattern-rule machinery, so this one invocation exercises both
    the compile and the static BTF inspection paths. A separate
    bpf-compile test would be redundant (make caches object timestamps
    and the second invocation would be a no-op).
    """
    if not _BPF_SOURCES:
        pytest.skip("no BPF sources to verify")
    result = subprocess.run(
        ["make", "bpf-verify"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        # Cold CI runner: clang -target bpf compile (~5 s per file) +
        # bpftool btf dump (~1 s). 300 s headroom covers a dozen BPF
        # sources without flaking.
        timeout=300,
    )
    assert result.returncode == 0, (
        f"bpf-verify failed (rc={result.returncode}):\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_bpf_objects_exist_after_compile() -> None:
    """Every src/*.bpf.c produces a build/*.bpf.o of nonzero size.

    Separate from bpf-verify because the artifact-existence check is
    a distinct invariant from BTF correctness: a hypothetical future
    Makefile change that skips the compile would pass bpf-verify (no
    objects to check) while failing this test.
    """
    if not _BPF_SOURCES:
        pytest.skip("no BPF sources")
    result = subprocess.run(
        ["make", "bpf-compile"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"bpf-compile failed (rc={result.returncode}):\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    for src in _BPF_SOURCES:
        # Makefile pattern: src/%.bpf.c -> build/%.bpf.o.
        obj = _REPO_ROOT / "build" / f"{src.stem}.o"
        assert obj.is_file(), f"missing BPF object: {obj}"
        assert obj.stat().st_size > 0, f"empty BPF object: {obj}"
