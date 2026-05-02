"""D015 sub-commit 7j: tests for the perf-measurement producer."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# pylint: disable=protected-access

from iomoments_ontology import load_test_results_dag


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_producer() -> Any:
    path = _REPO_ROOT / "tooling" / "perf_producer.py"
    spec = importlib.util.spec_from_file_location(
        "iomoments_perf_producer", str(path),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["iomoments_perf_producer"] = mod
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


_GOOD_SUMMARY = (
    "kernel=6.17.0-22-generic\n"
    "variant=k4\n"
    "per_event_ns_issue=412.50\n"
    "events_issue=20015\n"
    "per_event_ns_complete=187.30\n"
    "events_complete=20015\n"
)


# --- _parse_summary ---------------------------------------------------


def test_parse_summary_extracts_key_value(tmp_path: Path) -> None:
    p = tmp_path / "summary.txt"
    p.write_text(_GOOD_SUMMARY, encoding="utf-8")
    parsed = _PRODUCER._parse_summary(p)
    assert parsed["kernel"] == "6.17.0-22-generic"
    assert parsed["variant"] == "k4"
    assert parsed["per_event_ns_issue"] == "412.50"
    assert parsed["events_complete"] == "20015"


def test_parse_summary_skips_blank_and_comment_lines(
    tmp_path: Path,
) -> None:
    p = tmp_path / "summary.txt"
    p.write_text(
        "# header comment\n"
        "\n"
        "kernel=v6.17\n"
        "  \n",
        encoding="utf-8",
    )
    parsed = _PRODUCER._parse_summary(p)
    assert parsed == {"kernel": "v6.17"}


def test_parse_summary_returns_empty_for_missing_file(
    tmp_path: Path,
) -> None:
    assert _PRODUCER._parse_summary(tmp_path / "absent.txt") == {}


# --- _build_result ----------------------------------------------------


def test_build_result_populates_measurements() -> None:
    summary = {
        "kernel": "6.17.0-22-generic", "variant": "k4",
        "per_event_ns_issue": "412.50",
        "events_issue": "20015",
        "per_event_ns_complete": "187.30",
        "events_complete": "20015",
    }
    result = _PRODUCER._build_result(
        summary, "a" * 40,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert result is not None
    assert result.measurements["per_event_ns_issue"] == 412.50
    assert result.measurements["per_event_ns_complete"] == 187.30
    assert result.measurements["events_issue"] == 20015
    assert result.measurements["events_complete"] == 20015


def test_build_result_carries_kernel_and_variant_into_env() -> None:
    summary = {
        "kernel": "6.17.0-22-generic", "variant": "k4",
        "per_event_ns_issue": "412.50",
    }
    result = _PRODUCER._build_result(
        summary, "a" * 40,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert result is not None
    assert result.environment.kind == "host-perf"
    assert result.environment.kernel == "6.17.0-22-generic"
    assert result.environment.flags["variant"] == "k4"
    assert "make bpf-overhead" in result.environment.fix_recipe


def test_build_result_returns_none_for_no_numeric_values() -> None:
    """Malformed summary with only metadata → no record (don't emit
    a measurements-empty TestResult)."""
    summary = {"kernel": "v6.17", "variant": "k4"}
    result = _PRODUCER._build_result(
        summary, "a" * 40,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert result is None


def test_build_result_skips_non_numeric_values() -> None:
    """A garbled value in one metric doesn't kill the whole record."""
    summary = {
        "kernel": "v6.17", "variant": "k4",
        "per_event_ns_issue": "not a number",
        "per_event_ns_complete": "187.30",
    }
    result = _PRODUCER._build_result(
        summary, "a" * 40,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert result is not None
    assert "per_event_ns_issue" not in result.measurements
    assert result.measurements["per_event_ns_complete"] == 187.30


# --- emit() end-to-end ------------------------------------------------


def test_emit_writes_dag(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    summary_path = tmp_path / "summary.txt"
    summary_path.write_text(_GOOD_SUMMARY, encoding="utf-8")
    dag_path = tmp_path / "tr.json"
    created = _PRODUCER.emit(summary_path, dag_path, tmp_path)
    assert created is True
    dag = load_test_results_dag(str(dag_path), project_name="iomoments")
    snap = dag.get_current_node().snapshot  # type: ignore[union-attr]
    assert len(snap.results) == 1
    measurements = snap.results[0].measurements
    assert measurements["per_event_ns_issue"] == 412.50


def test_emit_missing_summary_is_noop(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    dag_path = tmp_path / "tr.json"
    created = _PRODUCER.emit(
        tmp_path / "absent.txt", dag_path, tmp_path,
    )
    assert created is False
    assert not dag_path.exists()


def test_emit_outside_git_is_noop(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.txt"
    summary_path.write_text(_GOOD_SUMMARY, encoding="utf-8")
    dag_path = tmp_path / "tr.json"
    created = _PRODUCER.emit(summary_path, dag_path, tmp_path)
    assert created is False


def test_emit_dedupes_same_observation(tmp_path: Path) -> None:
    """Two consecutive emit() calls at the same git_sha → second is
    a content-hash dedup no-op (identity-preserving prune_and_add)."""
    _init_repo(tmp_path)
    summary_path = tmp_path / "summary.txt"
    summary_path.write_text(_GOOD_SUMMARY, encoding="utf-8")
    dag_path = tmp_path / "tr.json"
    first = _PRODUCER.emit(summary_path, dag_path, tmp_path)
    second = _PRODUCER.emit(summary_path, dag_path, tmp_path)
    assert first is True
    assert second is False


# --- main() -----------------------------------------------------------


def test_main_returns_zero_on_happy_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    summary_path = tmp_path / "summary.txt"
    summary_path.write_text(_GOOD_SUMMARY, encoding="utf-8")
    dag_path = tmp_path / "tr.json"
    rc = _PRODUCER.main([
        "--summary", str(summary_path),
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0


def test_main_returns_zero_on_noop(tmp_path: Path) -> None:
    """No summary → exit 0 (producer never gates the perf run)."""
    rc = _PRODUCER.main([
        "--summary", str(tmp_path / "absent.txt"),
        "--dag", str(tmp_path / "tr.json"),
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0
