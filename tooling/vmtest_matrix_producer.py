"""D015 sub-commit 7i: producer for the vmtest BPF-load matrix.

Replaces ``make bpf-test-vm-matrix``'s exit-on-first-failure loop
(which has been broken since v6.17 / v6.18 landed in the kernel set
— both reject k=4 by design per D014 §2) with a per-(kernel, variant)
verdict matrix that:

- Walks every ``~/kernel-images/vmlinuz-v*``.
- For each, attempts both k=4 (build/iomoments.bpf.o) and k=3
  (build/iomoments-k3.bpf.o) loads via vmtest + bpftool.
- Emits one TestResult per (kernel, variant) pair where load
  succeeded.
- Prints a verdict matrix at the end.
- Exits 0 iff every kernel has at-least-one-variant accepting (the
  operational guarantee iomoments commits to per D014's k3-fallback
  design). Exits 1 only if a kernel rejects BOTH variants — that's a
  real regression.

Verification-ref shape matches the AWS-probe producer: the producer
script itself is the ref (``tooling/vmtest_matrix_producer.py``);
variant + kernel live in EnvironmentSpec via ``flags["variant"]``
and ``env.kernel``.

Slow by nature (~30s × kernel-count × variant-count). Not pre-push
material; invoked manually before pushes that touch the BPF program
(``make bpf-test-vm-matrix``). The freshness audit surfaces stale
results when src/iomoments.bpf.c is edited and the matrix hasn't
been re-run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from audit_ontology import git_helpers
from iomoments_ontology import (
    EnvironmentSpec,
    TestResult,
    TestResultsSnapshot,
    prune_and_add_result,
    prune_test_results_dag_nodes,
    snapshot_test_results_if_changed,
    test_results_dag_transaction,
)


_VERIFICATION_REF = "tooling/vmtest_matrix_producer.py"
_FIX_RECIPE = "make bpf-test-vm-matrix"

# (variant_name, build-output basename)
_VARIANTS = (
    ("k4", "iomoments.bpf.o"),
    ("k3", "iomoments-k3.bpf.o"),
)


def _find_bpftool() -> str | None:
    """Mirror the Makefile's bpftool resolution."""
    base = Path("/usr/lib/linux-tools")
    if base.is_dir():
        candidates = sorted(base.glob("*/bpftool"))
        if candidates:
            return str(candidates[-1])
    return shutil.which("bpftool")


def _find_vmtest() -> str | None:
    """Mirror the Makefile's vmtest resolution."""
    cargo_bin = Path.home() / ".cargo" / "bin" / "vmtest"
    if cargo_bin.is_file():
        return str(cargo_bin)
    return shutil.which("vmtest")


def _run_one(
    vmtest_bin: str,
    bpftool_bin: str,
    kernel_image: Path,
    bpf_obj: Path,
    pin: str,
) -> int:
    """Invoke vmtest to load ``bpf_obj`` against ``kernel_image``.

    Returns the vmtest exit code. Verifier rejection inside the
    guest typically surfaces as a non-zero exit; pinning happens
    inside the guest's tmpfs and disappears at boot teardown.
    """
    try:
        result = subprocess.run(
            [
                vmtest_bin,
                "--kernel", str(kernel_image),
                "--",
                bpftool_bin, "prog", "load",
                str(bpf_obj), pin,
            ],
            capture_output=True, text=True,
            check=False, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return 124
    return result.returncode


def _kernel_label(kernel_image: Path) -> str:
    """Return the short ``v6.18`` form from ``vmlinuz-v6.18``."""
    name = kernel_image.name
    return name[len("vmlinuz-"):] if name.startswith("vmlinuz-") else name


def _build_results(
    kernels_dir: Path,
    build_dir: Path,
    head_sha: str,
    captured_at: datetime,
    vmtest_bin: str,
    bpftool_bin: str,
) -> tuple[list[TestResult], dict[tuple[str, str], int]]:
    """Run the matrix, return (passes, verdict-dict).

    verdict-dict maps (kernel_label, variant) → exit code so the
    caller can render the matrix and decide overall pass/fail.
    """
    env_template = EnvironmentSpec(
        kind="vmtest", fix_recipe=_FIX_RECIPE,
    )
    results: list[TestResult] = []
    verdicts: dict[tuple[str, str], int] = {}

    kernels = sorted(kernels_dir.glob("vmlinuz-v*"))
    for kernel in kernels:
        label = _kernel_label(kernel)
        for variant_name, obj_basename in _VARIANTS:
            obj = build_dir / obj_basename
            if not obj.is_file():
                verdicts[(label, variant_name)] = 127
                continue
            pin = f"/sys/fs/bpf/iomoments_{variant_name}_{os.getpid()}"
            print(
                f"  loading {variant_name} on {label}...",
                file=sys.stderr,
            )
            rc = _run_one(
                vmtest_bin, bpftool_bin, kernel, obj, pin,
            )
            verdicts[(label, variant_name)] = rc
            if rc == 0:
                env = env_template.model_copy(update={
                    "kernel": label,
                    "flags": {"variant": variant_name},
                })
                results.append(TestResult(
                    verification_ref=_VERIFICATION_REF,
                    environment=env,
                    outcome="pass",
                    captured_git_sha=head_sha,
                    captured_at=captured_at,
                ))
    return results, verdicts


def _print_matrix(verdicts: dict[tuple[str, str], int]) -> None:
    """Render the (kernel, variant) → verdict grid to stdout."""
    kernels = sorted({k for (k, _) in verdicts})
    variants = sorted({v for (_, v) in verdicts})
    print("\n=== vmtest BPF-load matrix ===")
    header = "kernel".ljust(10) + "  ".join(v.ljust(8) for v in variants)
    print(header)
    print("-" * len(header))
    for k in kernels:
        cells = []
        for v in variants:
            rc = verdicts.get((k, v))
            cells.append("ACCEPT  " if rc == 0 else "REJECT  ")
        print(k.ljust(10) + "  ".join(cells))


def _emit(
    new_results: list[TestResult], dag_path: Path,
) -> None:
    """Best-effort DAG write — never raises."""
    if not new_results:
        return
    try:
        with test_results_dag_transaction(
            str(dag_path), project_name="iomoments",
        ) as dag:
            current = dag.get_current_node()
            snapshot = (
                current.snapshot
                if current is not None
                else TestResultsSnapshot()
            )
            for r in new_results:
                snapshot = prune_and_add_result(snapshot, r)
            snapshot_test_results_if_changed(
                dag, snapshot, label="vmtest-matrix",
            )
            prune_test_results_dag_nodes(dag)
    except Exception:  # pylint: disable=broad-except
        traceback.print_exc()


def _every_kernel_has_one_pass(
    verdicts: dict[tuple[str, str], int],
) -> bool:
    """D014's operational guarantee: at least one variant per kernel."""
    kernels = {k for (k, _) in verdicts}
    for kernel in kernels:
        passing = [
            (k, v) for (k, v), rc in verdicts.items()
            if k == kernel and rc == 0
        ]
        if not passing:
            return False
    return True


def _resolve_tools() -> tuple[str | None, str | None, int]:
    """Return (vmtest_bin, bpftool_bin, exit_code).

    exit_code != 0 means a tooling dependency is missing and the
    caller should bail with that code (2 = tooling error, distinct
    from "matrix found a regression" which is exit 1).
    """
    vmtest_bin = _find_vmtest()
    bpftool_bin = _find_bpftool()
    if vmtest_bin is None:
        print(
            "ERROR: vmtest not found (cargo install vmtest).",
            file=sys.stderr,
        )
        return None, None, 2
    if bpftool_bin is None:
        print(
            "ERROR: bpftool not found "
            "(apt install linux-tools-generic).",
            file=sys.stderr,
        )
        return None, None, 2
    return vmtest_bin, bpftool_bin, 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit 1 iff some kernel rejects EVERY variant."""
    repo_root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Sweep vmtest kernels × BPF variants, emit DAG.",
    )
    parser.add_argument(
        "--kernels-dir", type=Path,
        default=Path.home() / "kernel-images",
    )
    parser.add_argument(
        "--build-dir", type=Path,
        default=repo_root_default / "build",
    )
    parser.add_argument(
        "--dag", type=Path,
        default=(
            repo_root_default
            / "tooling"
            / "iomoments-test-results.json"
        ),
    )
    parser.add_argument(
        "--repo-root", type=Path,
        default=repo_root_default,
    )
    args = parser.parse_args(argv)

    vmtest_bin, bpftool_bin, tooling_rc = _resolve_tools()
    if tooling_rc != 0:
        return tooling_rc
    assert vmtest_bin is not None and bpftool_bin is not None

    head = git_helpers.head_sha(args.repo_root)
    captured_at = datetime.now(timezone.utc)

    results, verdicts = _build_results(
        args.kernels_dir, args.build_dir,
        head or "0" * 40, captured_at,
        vmtest_bin, bpftool_bin,
    )
    _print_matrix(verdicts)

    if head is not None and results:
        _emit(results, args.dag)
        print(
            f"\nvmtest_matrix_producer: emitted {len(results)} "
            f"TestResult(s) to {args.dag}",
        )

    if not verdicts:
        print(
            "WARN: no kernels found in "
            f"{args.kernels_dir} (no vmlinuz-v* matches).",
            file=sys.stderr,
        )
        return 0

    if not _every_kernel_has_one_pass(verdicts):
        bad = sorted({
            k for (k, _), rc in verdicts.items() if rc == 0
        } ^ {k for (k, _) in verdicts})
        print(
            f"\nERROR: kernel(s) rejected every variant: {bad}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
