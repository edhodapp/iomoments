"""D015 sub-commit 7i: tests for the vmtest matrix producer.

Real vmtest invocations take ~30s × kernel-count × variant-count
(~6 minutes for the iomoments matrix), so the tests mock
subprocess.run to avoid the cost. The mock surface is small:
_run_one is a single subprocess wrapper; _build_results loops over
(kernel, variant) combinations and calls _run_one. Tests exercise
the loop's bookkeeping plus the verdict-matrix bottom-line logic
(_every_kernel_has_one_pass).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

# pylint: disable=protected-access

from iomoments_ontology import (
    EnvironmentSpec,
    TestResult,
    TestResultsSnapshot,
    load_test_results_dag,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_producer() -> Any:
    path = _REPO_ROOT / "tooling" / "vmtest_matrix_producer.py"
    spec = importlib.util.spec_from_file_location(
        "iomoments_vmtest_matrix_producer", str(path),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["iomoments_vmtest_matrix_producer"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_PRODUCER = _import_producer()


def _init_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "x.txt").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "add", "x.txt"], cwd=path,
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=path,
        check=True, capture_output=True,
    )


def _seed_matrix(
    kernels_dir: Path, build_dir: Path, kernel_versions: list[str],
) -> None:
    """Create the file layout the producer reads.

    Doesn't matter what's IN the kernel images or .bpf.o files —
    the tests mock _run_one; the producer just checks the files
    exist and uses their paths.
    """
    kernels_dir.mkdir(parents=True, exist_ok=True)
    for v in kernel_versions:
        (kernels_dir / f"vmlinuz-{v}").write_text("kernel", encoding="utf-8")
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "iomoments.bpf.o").write_text("k4", encoding="utf-8")
    (build_dir / "iomoments-k3.bpf.o").write_text("k3", encoding="utf-8")


# --- _kernel_label -----------------------------------------------------


def test_version_key_orders_versions_correctly() -> None:
    """5.15 must sort AFTER 5.4 (the bug plain sorted() would have)."""
    paths = [
        Path("/usr/lib/linux-tools/5.4.0-100/bpftool"),
        Path("/usr/lib/linux-tools/5.15.0-50/bpftool"),
        Path("/usr/lib/linux-tools/6.1.0/bpftool"),
        Path("/usr/lib/linux-tools/5.15.0-130/bpftool"),
    ]
    sorted_paths = sorted(paths, key=_PRODUCER._version_key)
    versions = [p.parent.name for p in sorted_paths]
    assert versions == [
        "5.4.0-100",
        "5.15.0-50",
        "5.15.0-130",
        "6.1.0",
    ]


def test_kernel_label_strips_vmlinuz_prefix() -> None:
    assert _PRODUCER._kernel_label(Path("/foo/vmlinuz-v6.18")) == "v6.18"


def test_kernel_label_falls_back_to_basename() -> None:
    assert _PRODUCER._kernel_label(Path("/foo/something")) == "something"


# --- _every_kernel_has_one_pass ---------------------------------------


def test_every_kernel_has_one_pass_all_pass() -> None:
    verdicts = {
        ("v5.15", "k4"): 0, ("v5.15", "k3"): 0,
        ("v6.18", "k4"): 255, ("v6.18", "k3"): 0,
    }
    assert _PRODUCER._every_kernel_has_one_pass(verdicts) is True


def test_every_kernel_has_one_pass_one_kernel_fails_both() -> None:
    """v6.18 rejecting BOTH variants is the regression signal."""
    verdicts = {
        ("v5.15", "k4"): 0, ("v5.15", "k3"): 0,
        ("v6.18", "k4"): 255, ("v6.18", "k3"): 255,
    }
    assert _PRODUCER._every_kernel_has_one_pass(verdicts) is False


def test_every_kernel_has_one_pass_only_k3_path() -> None:
    """k3 alone passing is fine — that's the fallback's job."""
    verdicts = {
        ("v6.18", "k4"): 255, ("v6.18", "k3"): 0,
    }
    assert _PRODUCER._every_kernel_has_one_pass(verdicts) is True


# --- _build_results (mocked _run_one) ---------------------------------


def test_build_results_emits_one_per_passing_pair(tmp_path: Path) -> None:
    kernels_dir = tmp_path / "kernels"
    build_dir = tmp_path / "build"
    _seed_matrix(kernels_dir, build_dir, ["v5.15", "v6.18"])

    # k4 passes on v5.15, fails on v6.18; k3 passes on both.
    # pylint: disable=unused-argument
    def fake_run(
        vmtest: str, bpftool: str,
        kernel: Path, obj: Path, pin: str,
    ) -> int:
        kernel_label = kernel.name[len("vmlinuz-"):]
        if "k3" in obj.name:
            return 0
        return 0 if kernel_label == "v5.15" else 255

    with patch.object(_PRODUCER, "_run_one", side_effect=fake_run):
        results, verdicts = _PRODUCER._build_results(
            kernels_dir, build_dir,
            "a" * 40,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            "/fake/vmtest", "/fake/bpftool",
        )

    assert len(results) == 3  # v5.15-k4, v5.15-k3, v6.18-k3
    by_pair = {(r.environment.kernel, r.environment.flags["variant"])
               for r in results}
    assert by_pair == {("v5.15", "k4"), ("v5.15", "k3"), ("v6.18", "k3")}
    assert verdicts == {
        ("v5.15", "k4"): 0,
        ("v5.15", "k3"): 0,
        ("v6.18", "k4"): 255,
        ("v6.18", "k3"): 0,
    }


def test_build_results_carries_kernel_into_env(tmp_path: Path) -> None:
    kernels_dir = tmp_path / "kernels"
    build_dir = tmp_path / "build"
    _seed_matrix(kernels_dir, build_dir, ["v6.18"])
    with patch.object(_PRODUCER, "_run_one", return_value=0):
        results, _ = _PRODUCER._build_results(
            kernels_dir, build_dir,
            "a" * 40,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            "/fake/vmtest", "/fake/bpftool",
        )
    env = results[0].environment
    assert env.kind == "vmtest"
    assert env.kernel == "v6.18"
    assert env.fix_recipe == "make bpf-test-vm-matrix"


def test_build_results_uses_producer_verification_ref(
    tmp_path: Path,
) -> None:
    """All vmtest matrix records share the producer-script ref."""
    kernels_dir = tmp_path / "kernels"
    build_dir = tmp_path / "build"
    _seed_matrix(kernels_dir, build_dir, ["v6.18"])
    with patch.object(_PRODUCER, "_run_one", return_value=0):
        results, _ = _PRODUCER._build_results(
            kernels_dir, build_dir,
            "a" * 40,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            "/fake/vmtest", "/fake/bpftool",
        )
    refs = {r.verification_ref for r in results}
    assert refs == {"tooling/vmtest_matrix_producer.py"}


def test_build_results_records_missing_bpf_obj(tmp_path: Path) -> None:
    """Missing .bpf.o → verdict 127, no TestResult emitted."""
    kernels_dir = tmp_path / "kernels"
    build_dir = tmp_path / "build"
    kernels_dir.mkdir()
    (kernels_dir / "vmlinuz-v5.15").write_text("k", encoding="utf-8")
    build_dir.mkdir()
    # NOTE: deliberately don't create iomoments.bpf.o or iomoments-k3.bpf.o.
    with patch.object(_PRODUCER, "_run_one", return_value=0) as run_mock:
        results, verdicts = _PRODUCER._build_results(
            kernels_dir, build_dir,
            "a" * 40,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            "/fake/vmtest", "/fake/bpftool",
        )
    assert results == []
    assert verdicts == {("v5.15", "k4"): 127, ("v5.15", "k3"): 127}
    run_mock.assert_not_called()


# --- main() end-to-end (mocked) ---------------------------------------


def test_purge_drops_records_for_tested_failing_envs() -> None:
    """Stale-pass-survives-regression bug fix: if a previously-
    passing env starts failing, the producer must purge the prior
    record, not leave it to falsely satisfy the freshness audit.
    """
    # Prior snapshot: v6.12 k=4 passed at SHA "old".
    prior_env = EnvironmentSpec(
        kind="vmtest", kernel="v6.12",
        flags={"variant": "k4"}, fix_recipe="make bpf-test-vm-matrix",
    )
    unrelated_env = EnvironmentSpec(
        kind="host", fix_recipe=".venv/bin/pytest {ref}",
    )
    prior_snapshot = TestResultsSnapshot(results=[
        TestResult(
            verification_ref="tooling/vmtest_matrix_producer.py",
            environment=prior_env, outcome="pass",
            captured_git_sha="o" * 40,
            captured_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        ),
        # An unrelated record (different ref) must NOT be touched.
        TestResult(
            verification_ref="tests/test_x.py::test_y",
            environment=unrelated_env, outcome="pass",
            captured_git_sha="o" * 40,
            captured_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        ),
    ])
    # Current run: v6.12 k=4 failed (rc=255).
    verdicts = {("v6.12", "k4"): 255}
    out = _PRODUCER._purge_stale_for_failed_envs(
        prior_snapshot, verdicts,
    )
    refs = {r.verification_ref for r in out.results}
    assert "tooling/vmtest_matrix_producer.py" not in refs
    assert "tests/test_x.py::test_y" in refs


def test_purge_no_op_when_all_passed() -> None:
    """No tested-but-failing envs → snapshot unchanged."""
    prior_snapshot = TestResultsSnapshot(results=[
        TestResult(
            verification_ref="tooling/vmtest_matrix_producer.py",
            environment=EnvironmentSpec(
                kind="vmtest", kernel="v5.15",
                flags={"variant": "k4"},
                fix_recipe="make bpf-test-vm-matrix",
            ),
            outcome="pass", captured_git_sha="a" * 40,
            captured_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ),
    ])
    verdicts = {("v5.15", "k4"): 0}  # passed
    out = _PRODUCER._purge_stale_for_failed_envs(
        prior_snapshot, verdicts,
    )
    assert out == prior_snapshot


def test_purge_preserves_records_for_envs_we_didnt_test() -> None:
    """A record for an env outside the verdict dict is preserved."""
    prior_snapshot = TestResultsSnapshot(results=[
        TestResult(
            verification_ref="tooling/vmtest_matrix_producer.py",
            environment=EnvironmentSpec(
                kind="vmtest", kernel="v5.15",
                flags={"variant": "k4"},
                fix_recipe="make bpf-test-vm-matrix",
            ),
            outcome="pass", captured_git_sha="a" * 40,
            captured_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ),
    ])
    # We tested v6.18 only (and it failed); v5.15 wasn't on the
    # menu. Its prior pass survives.
    verdicts = {("v6.18", "k4"): 255}
    out = _PRODUCER._purge_stale_for_failed_envs(
        prior_snapshot, verdicts,
    )
    assert len(out.results) == 1
    assert out.results[0].environment.kernel == "v5.15"


def test_main_returns_zero_when_every_kernel_has_one_pass(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    kernels_dir = tmp_path / "kernels"
    build_dir = tmp_path / "build"
    _seed_matrix(kernels_dir, build_dir, ["v5.15", "v6.18"])
    dag_path = tmp_path / "tr.json"

    # pylint: disable=unused-argument
    def fake_run(
        vmtest: str, bpftool: str,
        kernel: Path, obj: Path, pin: str,
    ) -> int:
        if "k3" in obj.name:
            return 0
        return 0 if kernel.name.endswith("v5.15") else 255

    fake_vm = patch.object(
        _PRODUCER, "_find_vmtest", return_value="/fake/vmtest",
    )
    fake_bp = patch.object(
        _PRODUCER, "_find_bpftool", return_value="/fake/bpftool",
    )
    fake_run_p = patch.object(
        _PRODUCER, "_run_one", side_effect=fake_run,
    )
    with fake_vm, fake_bp, fake_run_p:
        rc = _PRODUCER.main([
            "--kernels-dir", str(kernels_dir),
            "--build-dir", str(build_dir),
            "--dag", str(dag_path),
            "--repo-root", str(tmp_path),
        ])
    assert rc == 0
    dag = load_test_results_dag(str(dag_path), project_name="iomoments")
    snap = dag.get_current_node().snapshot  # type: ignore[union-attr]
    # 3 records: v5.15-k4, v5.15-k3, v6.18-k3.
    assert len(snap.results) == 3


def test_main_returns_one_when_kernel_rejects_all_variants(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    kernels_dir = tmp_path / "kernels"
    build_dir = tmp_path / "build"
    _seed_matrix(kernels_dir, build_dir, ["v9.0"])
    dag_path = tmp_path / "tr.json"

    fake_vm = patch.object(
        _PRODUCER, "_find_vmtest", return_value="/fake/vmtest",
    )
    fake_bp = patch.object(
        _PRODUCER, "_find_bpftool", return_value="/fake/bpftool",
    )
    with patch.object(_PRODUCER, "_run_one", return_value=255), \
            fake_vm, fake_bp:
        rc = _PRODUCER.main([
            "--kernels-dir", str(kernels_dir),
            "--build-dir", str(build_dir),
            "--dag", str(dag_path),
            "--repo-root", str(tmp_path),
        ])
    assert rc == 1


def test_main_returns_two_when_vmtest_missing(tmp_path: Path) -> None:
    """Tooling error (vmtest not installed) → exit 2 not 1."""
    _init_repo(tmp_path)
    kernels_dir = tmp_path / "kernels"
    build_dir = tmp_path / "build"
    _seed_matrix(kernels_dir, build_dir, ["v5.15"])
    fake_bp = patch.object(
        _PRODUCER, "_find_bpftool", return_value="/fake/bpftool",
    )
    with patch.object(_PRODUCER, "_find_vmtest", return_value=None), \
            fake_bp:
        rc = _PRODUCER.main([
            "--kernels-dir", str(kernels_dir),
            "--build-dir", str(build_dir),
            "--dag", str(tmp_path / "tr.json"),
            "--repo-root", str(tmp_path),
        ])
    assert rc == 2


def test_main_returns_zero_on_empty_kernels_dir(tmp_path: Path) -> None:
    """No kernels in dir → warn + exit 0 (matrix is empty, not failed)."""
    _init_repo(tmp_path)
    kernels_dir = tmp_path / "kernels"
    kernels_dir.mkdir()
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    fake_vm = patch.object(
        _PRODUCER, "_find_vmtest", return_value="/fake/vmtest",
    )
    fake_bp = patch.object(
        _PRODUCER, "_find_bpftool", return_value="/fake/bpftool",
    )
    with fake_vm, fake_bp:
        rc = _PRODUCER.main([
            "--kernels-dir", str(kernels_dir),
            "--build-dir", str(build_dir),
            "--dag", str(tmp_path / "tr.json"),
            "--repo-root", str(tmp_path),
        ])
    assert rc == 0
