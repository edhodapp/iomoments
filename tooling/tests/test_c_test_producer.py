"""D015 sub-commit 7h: tests for the C-test producer."""

from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# pylint: disable=protected-access

from iomoments_ontology import load_test_results_dag


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_producer() -> Any:
    path = _REPO_ROOT / "tooling" / "c_test_producer.py"
    spec = importlib.util.spec_from_file_location(
        "iomoments_c_test_producer", str(path),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["iomoments_c_test_producer"] = mod
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


def _make_pass_binary(path: Path, stdout_text: str = "ok\n") -> None:
    """Write a tiny shell script that prints + exits 0."""
    path.write_text(
        f"#!/bin/sh\necho '{stdout_text.rstrip()}'\nexit 0\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _make_fail_binary(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\necho 'failure msg' >&2\nexit 1\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# --- _discover_test_functions -----------------------------------------


def test_discover_finds_void_void_pattern(tmp_path: Path) -> None:
    """Standard iomoments shape: ``static void test_X(void)``."""
    src = tmp_path / "test_x.c"
    src.write_text(
        "#include <stdio.h>\n"
        "static void test_alpha(void) { /* ... */ }\n"
        "static void test_beta(void) { /* ... */ }\n"
        "int main(void) { test_alpha(); test_beta(); return 0; }\n",
        encoding="utf-8",
    )
    fns = _PRODUCER._discover_test_functions(src)
    assert fns == ["test_alpha", "test_beta"]


def test_discover_handles_no_static_keyword(tmp_path: Path) -> None:
    """``void test_x(void)`` (no static) also matches."""
    src = tmp_path / "test_x.c"
    src.write_text(
        "void test_alpha(void) { /* ... */ }\n"
        "int main(void) { test_alpha(); return 0; }\n",
        encoding="utf-8",
    )
    fns = _PRODUCER._discover_test_functions(src)
    assert fns == ["test_alpha"]


def test_discover_skips_non_test_functions(tmp_path: Path) -> None:
    src = tmp_path / "test_x.c"
    src.write_text(
        "static void helper(void) { }\n"
        "static void test_real(void) { }\n"
        "int main(void) { test_real(); return 0; }\n",
        encoding="utf-8",
    )
    assert _PRODUCER._discover_test_functions(src) == ["test_real"]


def test_discover_returns_empty_for_missing_file(tmp_path: Path) -> None:
    assert _PRODUCER._discover_test_functions(
        tmp_path / "absent.c",
    ) == []


def test_discover_skips_function_calls(tmp_path: Path) -> None:
    """Regex must reject ``test_x();`` (a call) inside main()."""
    src = tmp_path / "test_x.c"
    src.write_text(
        "static void test_alpha(void) { }\n"
        "int main(void) {\n"
        "    test_alpha();  /* call, not a definition */\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    fns = _PRODUCER._discover_test_functions(src)
    # One definition seen — call below doesn't get double-counted.
    assert fns == ["test_alpha"]


# --- _run_binary -------------------------------------------------------


def test_run_binary_pass(tmp_path: Path) -> None:
    bin_path = tmp_path / "binary"
    _make_pass_binary(bin_path, "all good")
    rc, stdout, _ = _PRODUCER._run_binary(bin_path)
    assert rc == 0
    assert "all good" in stdout


def test_run_binary_fail(tmp_path: Path) -> None:
    bin_path = tmp_path / "binary"
    _make_fail_binary(bin_path)
    rc, _, stderr = _PRODUCER._run_binary(bin_path)
    assert rc == 1
    assert "failure msg" in stderr


def test_run_binary_missing_returns_127(tmp_path: Path) -> None:
    rc, _, stderr = _PRODUCER._run_binary(tmp_path / "absent")
    assert rc == 127
    assert "not found" in stderr


# --- _build_results ----------------------------------------------------


def test_build_results_pass_emits_one_per_function(
    tmp_path: Path,
) -> None:
    """A passing binary contributes one TestResult per test_* in its source."""
    test_dir = tmp_path / "tests" / "c"
    test_dir.mkdir(parents=True)
    (test_dir / "test_x.c").write_text(
        "static void test_a(void) { }\n"
        "static void test_b(void) { }\n",
        encoding="utf-8",
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _make_pass_binary(build_dir / "test_x")

    results, failed, missing = _PRODUCER._build_results(
        test_dir, build_dir, "a" * 40,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert failed == []
    assert missing == []
    assert len(results) == 2
    refs = sorted(r.verification_ref for r in results)
    assert refs == [
        "tests/c/test_x.c:test_a",
        "tests/c/test_x.c:test_b",
    ]


def test_build_results_failed_binary_emits_nothing(
    tmp_path: Path,
) -> None:
    test_dir = tmp_path / "tests" / "c"
    test_dir.mkdir(parents=True)
    (test_dir / "test_x.c").write_text(
        "static void test_a(void) { }\n", encoding="utf-8",
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _make_fail_binary(build_dir / "test_x")

    results, failed, missing = _PRODUCER._build_results(
        test_dir, build_dir, "a" * 40,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert results == []
    assert len(failed) == 1
    assert missing == []


def test_build_results_missing_binary_recorded(tmp_path: Path) -> None:
    test_dir = tmp_path / "tests" / "c"
    test_dir.mkdir(parents=True)
    (test_dir / "test_x.c").write_text(
        "static void test_a(void) { }\n", encoding="utf-8",
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    # NOTE: deliberately don't create build/test_x

    results, failed, missing = _PRODUCER._build_results(
        test_dir, build_dir, "a" * 40,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert results == []
    assert failed == []
    assert len(missing) == 1


def test_build_results_uses_host_env_with_make_recipe(
    tmp_path: Path,
) -> None:
    test_dir = tmp_path / "tests" / "c"
    test_dir.mkdir(parents=True)
    (test_dir / "test_x.c").write_text(
        "static void test_a(void) { }\n", encoding="utf-8",
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _make_pass_binary(build_dir / "test_x")

    results, _, _ = _PRODUCER._build_results(
        test_dir, build_dir, "a" * 40,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    env = results[0].environment
    assert env.kind == "host"
    assert env.fix_recipe == "make test-c"


# --- main() end-to-end -------------------------------------------------


def test_main_returns_zero_on_all_pass(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    test_dir = tmp_path / "tests" / "c"
    test_dir.mkdir(parents=True)
    (test_dir / "test_x.c").write_text(
        "static void test_a(void) { }\n", encoding="utf-8",
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _make_pass_binary(build_dir / "test_x", "ok")
    dag_path = tmp_path / "tr.json"

    rc = _PRODUCER.main([
        "--build-dir", str(build_dir),
        "--test-dir", str(test_dir),
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0
    dag = load_test_results_dag(str(dag_path), project_name="iomoments")
    snap = dag.get_current_node().snapshot  # type: ignore[union-attr]
    assert len(snap.results) == 1


def test_main_returns_one_on_any_fail(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    test_dir = tmp_path / "tests" / "c"
    test_dir.mkdir(parents=True)
    (test_dir / "test_x.c").write_text(
        "static void test_a(void) { }\n", encoding="utf-8",
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _make_fail_binary(build_dir / "test_x")
    dag_path = tmp_path / "tr.json"

    rc = _PRODUCER.main([
        "--build-dir", str(build_dir),
        "--test-dir", str(test_dir),
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
    ])
    assert rc == 1
    # Failed binary → no DAG records emitted.
    assert not dag_path.exists()


def test_main_outside_git_skips_dag_write(tmp_path: Path) -> None:
    """No git head → producer doesn't crash, just skips DAG."""
    test_dir = tmp_path / "tests" / "c"
    test_dir.mkdir(parents=True)
    (test_dir / "test_x.c").write_text(
        "static void test_a(void) { }\n", encoding="utf-8",
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _make_pass_binary(build_dir / "test_x")
    dag_path = tmp_path / "tr.json"
    rc = _PRODUCER.main([
        "--build-dir", str(build_dir),
        "--test-dir", str(test_dir),
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
    ])
    # Binary passed → exit 0; no git head → no DAG file.
    assert rc == 0
    assert not dag_path.exists()
