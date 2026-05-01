"""D015 sub-commit 7g: tests for the AWS-probe producer.

Exercises the producer's parsing helpers (meta.txt, *.verdict),
the per-(distro, variant) TestResult construction, and the end-
to-end emit() against a tmp DAG with a real tmp git repo.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# pylint: disable=protected-access

from iomoments_ontology import load_test_results_dag


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_producer() -> Any:
    """Load the producer script as a module by file path."""
    path = _REPO_ROOT / "tooling" / "aws_tracer_producer.py"
    spec = importlib.util.spec_from_file_location(
        "iomoments_aws_tracer_producer", str(path),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["iomoments_aws_tracer_producer"] = mod
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


def _seed_distro(
    results_dir: Path,
    distro: str,
    kernel: str,
    k4_verdict: int,
    k3_verdict: int,
) -> None:
    """Build the file layout the producer reads, per distro."""
    distro_dir = results_dir / distro
    distro_dir.mkdir(parents=True, exist_ok=True)
    meta = (
        f"{kernel}\n"
        f"Linux ip-1-2-3-4 {kernel} #1 SMP\n"
        f'NAME="distro"\n'
        f'VERSION_ID="x"\n'
        f"AMI=ami-deadbeef\n"
        f"InstanceId=i-feedface\n"
    )
    (distro_dir / "meta.txt").write_text(meta, encoding="utf-8")
    (distro_dir / "k4.verdict").write_text(
        f"VERDICT={k4_verdict}\n", encoding="utf-8",
    )
    (distro_dir / "k3.verdict").write_text(
        f"VERDICT={k3_verdict}\n", encoding="utf-8",
    )


# --- _parse_meta -------------------------------------------------------


def test_parse_meta_extracts_kernel(tmp_path: Path) -> None:
    meta = tmp_path / "meta.txt"
    meta.write_text(
        "5.15.0-1084-aws\n"
        "Linux ip 5.15.0 #1\n"
        'NAME="Ubuntu"\n'
        'VERSION_ID="20.04"\n',
        encoding="utf-8",
    )
    parsed = _PRODUCER._parse_meta(meta)  # noqa: SLF001
    assert parsed["kernel"] == "5.15.0-1084-aws"
    assert parsed["NAME"] == "Ubuntu"
    assert parsed["VERSION_ID"] == "20.04"


def test_parse_meta_handles_missing_file(tmp_path: Path) -> None:
    parsed = _PRODUCER._parse_meta(tmp_path / "absent.txt")  # noqa: SLF001
    assert parsed == {}


# --- _parse_verdict ----------------------------------------------------


def test_parse_verdict_pass(tmp_path: Path) -> None:
    p = tmp_path / "v.verdict"
    p.write_text("VERDICT=0\n", encoding="utf-8")
    assert _PRODUCER._parse_verdict(p) == 0  # noqa: SLF001


def test_parse_verdict_reject(tmp_path: Path) -> None:
    p = tmp_path / "v.verdict"
    p.write_text("VERDICT=255\n", encoding="utf-8")
    assert _PRODUCER._parse_verdict(p) == 255  # noqa: SLF001


def test_parse_verdict_malformed_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "v.verdict"
    p.write_text("not a verdict line\n", encoding="utf-8")
    assert _PRODUCER._parse_verdict(p) is None  # noqa: SLF001


def test_parse_verdict_missing_file(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    assert _PRODUCER._parse_verdict(absent) is None  # noqa: SLF001


# --- _build_results ----------------------------------------------------


def test_build_results_emits_only_passing_variants(tmp_path: Path) -> None:
    """k4=255 (rejected) must NOT produce a TestResult; k3=0 must."""
    results_dir = tmp_path / "build" / "aws-tracer"
    _seed_distro(
        results_dir, "al2023", "6.18-amzn",
        k4_verdict=255, k3_verdict=0,
    )
    out = _PRODUCER._build_results(  # noqa: SLF001
        results_dir, "a" * 40, datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len(out) == 1
    assert out[0].environment.flags["variant"] == "k3"


def test_build_results_one_per_passing_pair(tmp_path: Path) -> None:
    """Three distros × 2 variants − 1 reject = 5 records."""
    results_dir = tmp_path / "build" / "aws-tracer"
    _seed_distro(
        results_dir, "ubuntu-20.04", "5.15-aws",
        k4_verdict=0, k3_verdict=0,
    )
    _seed_distro(
        results_dir, "ubuntu-22.04", "6.8-aws",
        k4_verdict=0, k3_verdict=0,
    )
    _seed_distro(
        results_dir, "al2023", "6.18-amzn",
        k4_verdict=255, k3_verdict=0,
    )
    out = _PRODUCER._build_results(  # noqa: SLF001
        results_dir, "a" * 40, datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len(out) == 5
    by_distro_variant = {
        (r.environment.distro, r.environment.flags["variant"])
        for r in out
    }
    assert by_distro_variant == {
        ("ubuntu-20.04", "k4"), ("ubuntu-20.04", "k3"),
        ("ubuntu-22.04", "k4"), ("ubuntu-22.04", "k3"),
        ("al2023", "k3"),
    }


def test_build_results_carries_kernel_and_distro_into_env(
    tmp_path: Path,
) -> None:
    results_dir = tmp_path / "build" / "aws-tracer"
    _seed_distro(
        results_dir, "ubuntu-20.04", "5.15.0-1084-aws",
        k4_verdict=0, k3_verdict=0,
    )
    out = _PRODUCER._build_results(  # noqa: SLF001
        results_dir, "a" * 40, datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    env = out[0].environment
    assert env.kind == "aws-ec2"
    assert env.kernel == "5.15.0-1084-aws"
    assert env.distro == "ubuntu-20.04"
    assert env.fix_recipe == "bash scripts/aws_tracer.sh"


def test_build_results_uses_file_only_verification_ref(
    tmp_path: Path,
) -> None:
    """All AWS-probe records share the file-only ref (no symbol)."""
    results_dir = tmp_path / "build" / "aws-tracer"
    _seed_distro(
        results_dir, "ubuntu-20.04", "5.15-aws",
        k4_verdict=0, k3_verdict=0,
    )
    out = _PRODUCER._build_results(  # noqa: SLF001
        results_dir, "a" * 40, datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    refs = {r.verification_ref for r in out}
    assert refs == {"scripts/aws_tracer.sh"}


# --- emit() end-to-end -------------------------------------------------


def test_emit_writes_dag_and_returns_count(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    results_dir = tmp_path / "build" / "aws-tracer"
    _seed_distro(
        results_dir, "ubuntu-20.04", "5.15-aws",
        k4_verdict=0, k3_verdict=0,
    )
    dag_path = tmp_path / "tr.json"
    count = _PRODUCER.emit(results_dir, dag_path, tmp_path)
    assert count == 2
    dag = load_test_results_dag(str(dag_path), project_name="iomoments")
    snap = dag.get_current_node().snapshot  # type: ignore[union-attr]
    assert len(snap.results) == 2


def test_emit_outside_git_is_noop(tmp_path: Path) -> None:
    """No git → no captured_git_sha → producer skips."""
    results_dir = tmp_path / "build" / "aws-tracer"
    _seed_distro(
        results_dir, "ubuntu-20.04", "5.15-aws",
        k4_verdict=0, k3_verdict=0,
    )
    dag_path = tmp_path / "tr.json"
    count = _PRODUCER.emit(results_dir, dag_path, tmp_path)
    assert count == 0
    assert not dag_path.exists()


def test_emit_no_results_dir_is_noop(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    dag_path = tmp_path / "tr.json"
    count = _PRODUCER.emit(
        tmp_path / "absent", dag_path, tmp_path,
    )
    assert count == 0
    assert not dag_path.exists()


def test_emit_all_rejected_is_noop(tmp_path: Path) -> None:
    """If every variant rejected, no TestResults emitted."""
    _init_repo(tmp_path)
    results_dir = tmp_path / "build" / "aws-tracer"
    _seed_distro(
        results_dir, "future-strict", "9.0",
        k4_verdict=255, k3_verdict=255,
    )
    dag_path = tmp_path / "tr.json"
    count = _PRODUCER.emit(results_dir, dag_path, tmp_path)
    assert count == 0


# --- CLI ---------------------------------------------------------------


def test_main_returns_zero_on_happy_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _init_repo(tmp_path)
    results_dir = tmp_path / "build" / "aws-tracer"
    _seed_distro(
        results_dir, "ubuntu-20.04", "5.15-aws",
        k4_verdict=0, k3_verdict=0,
    )
    dag_path = tmp_path / "tr.json"
    rc = _PRODUCER.main([
        "--results-dir", str(results_dir),
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0
    assert "emitted 2 TestResult" in capsys.readouterr().out


def test_main_returns_zero_on_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """No results-dir → exit 0 (producer never gates anything)."""
    rc = _PRODUCER.main([
        "--results-dir", str(tmp_path / "absent"),
        "--dag", str(tmp_path / "tr.json"),
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0
    assert "emitted 0 TestResult" in capsys.readouterr().out
