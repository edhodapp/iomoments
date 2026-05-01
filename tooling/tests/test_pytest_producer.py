"""D015 sub-commit 7f: tests for the pytest producer (conftest.py).

Tests the producer's hook surface in isolation rather than running
pytest-in-pytest:

- emit_snapshot writes to a tmp DAG and returns True iff the snapshot
  was actually appended.
- Empty passed_nodeids → no-op (False return, no DAG written).
- Outside a git repo → no-op (no captured_git_sha to commit).
- Identical passes on two consecutive calls → second call is a content-
  hash dedup no-op.
- Captured TestResult carries the right fix_recipe template.
- _producer_disabled honors IOMOMENTS_TEST_RESULTS_DAG_DISABLE=1.

Note: ``conftest.py`` lives at the repo root. Importing it from a test
module requires sys.path manipulation; the simpler path is to import
the module via importlib by file path.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from iomoments_ontology import load_test_results_dag


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_conftest() -> Any:
    """Load the repo-root conftest.py as a module by path."""
    path = _REPO_ROOT / "conftest.py"
    spec = importlib.util.spec_from_file_location(
        "iomoments_root_conftest", str(path),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["iomoments_root_conftest"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_CONFTEST = _import_conftest()


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


# --- emit_snapshot -----------------------------------------------------


def test_emit_snapshot_writes_dag(tmp_path: Path) -> None:
    """Happy path: list of passes → DAG with one snapshot."""
    _init_repo(tmp_path)
    dag_path = tmp_path / "tr.json"
    created = _CONFTEST.emit_snapshot(
        ["tests/x.py::test_a", "tests/x.py::test_b"],
        dag_path, tmp_path,
    )
    assert created is True
    assert dag_path.exists()
    dag = load_test_results_dag(str(dag_path), project_name="iomoments")
    assert len(dag.nodes) == 1
    snap = dag.get_current_node().snapshot  # type: ignore[union-attr]
    assert len(snap.results) == 2
    assert {r.verification_ref for r in snap.results} == {
        "tests/x.py::test_a", "tests/x.py::test_b",
    }


def test_emit_snapshot_empty_passes_is_noop(tmp_path: Path) -> None:
    """No passes → no DAG file written."""
    _init_repo(tmp_path)
    dag_path = tmp_path / "tr.json"
    created = _CONFTEST.emit_snapshot([], dag_path, tmp_path)
    assert created is False
    assert not dag_path.exists()


def test_emit_snapshot_outside_git_is_noop(tmp_path: Path) -> None:
    """No git repo → no captured_git_sha → no write."""
    dag_path = tmp_path / "tr.json"
    created = _CONFTEST.emit_snapshot(
        ["tests/x.py::test_a"], dag_path, tmp_path,
    )
    assert created is False
    assert not dag_path.exists()


def test_emit_snapshot_dedupes_identical_calls(tmp_path: Path) -> None:
    """Two identical calls → first writes, second is content-hash no-op."""
    _init_repo(tmp_path)
    dag_path = tmp_path / "tr.json"
    nodeids = ["tests/x.py::test_a"]
    created1 = _CONFTEST.emit_snapshot(nodeids, dag_path, tmp_path)
    created2 = _CONFTEST.emit_snapshot(nodeids, dag_path, tmp_path)
    assert created1 is True
    assert created2 is False, (
        "second identical call must be content-hash deduped"
    )
    dag = load_test_results_dag(str(dag_path), project_name="iomoments")
    assert len(dag.nodes) == 1


def test_emit_snapshot_writes_pytest_fix_recipe(tmp_path: Path) -> None:
    """Producer fills in EnvironmentSpec.fix_recipe with a pytest re-run.

    D015 §5 audit failure messages interpolate this template into
    the 'fix:' line so the user sees the exact command to run.
    """
    _init_repo(tmp_path)
    dag_path = tmp_path / "tr.json"
    _CONFTEST.emit_snapshot(
        ["tests/x.py::test_a"], dag_path, tmp_path,
    )
    dag = load_test_results_dag(str(dag_path), project_name="iomoments")
    snap = dag.get_current_node().snapshot  # type: ignore[union-attr]
    result = snap.results[0]
    assert result.environment.kind == "host"
    assert ".venv/bin/pytest" in result.environment.fix_recipe
    assert "{ref}" in result.environment.fix_recipe


# --- _producer_disabled -----------------------------------------------


def test_producer_disabled_default_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default behavior: producer enabled."""
    monkeypatch.delenv(
        "IOMOMENTS_TEST_RESULTS_DAG_DISABLE", raising=False,
    )
    # pylint: disable=protected-access
    assert _CONFTEST._producer_disabled() is False  # noqa: SLF001


def test_producer_disabled_with_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IOMOMENTS_TEST_RESULTS_DAG_DISABLE=1 disables the producer."""
    monkeypatch.setenv("IOMOMENTS_TEST_RESULTS_DAG_DISABLE", "1")
    # pylint: disable=protected-access
    assert _CONFTEST._producer_disabled() is True  # noqa: SLF001


def test_producer_disabled_with_other_value_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything other than '1' leaves the producer enabled."""
    monkeypatch.setenv(
        "IOMOMENTS_TEST_RESULTS_DAG_DISABLE", "true",
    )
    # pylint: disable=protected-access
    assert _CONFTEST._producer_disabled() is False  # noqa: SLF001


# --- captured_git_sha matches HEAD ------------------------------------


def test_captured_git_sha_matches_repo_head(tmp_path: Path) -> None:
    """The captured_git_sha must equal HEAD at write time so the
    freshness rule's at-or-after check is meaningful."""
    _init_repo(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    dag_path = tmp_path / "tr.json"
    _CONFTEST.emit_snapshot(
        ["tests/x.py::test_a"], dag_path, tmp_path,
    )
    dag = load_test_results_dag(str(dag_path), project_name="iomoments")
    snap = dag.get_current_node().snapshot  # type: ignore[union-attr]
    assert snap.results[0].captured_git_sha == head
