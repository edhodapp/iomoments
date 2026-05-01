"""D015 sub-commit 7d: tests for the audit's freshness extension.

Exercises:
- audit_ontology.git_helpers (head_sha, last_touch_file,
  is_ancestor_of_head, is_at_or_after) against a real tmp git repo.
- audit_ontology.freshness._env_matches structural-subtype rule.
- audit_ontology.freshness.check_claim_freshness for each of D015
  §5's three failure modes plus the happy path.
- audit_ontology.audit.run_audit with enforce_freshness on/off and
  bootstrap on/off.
- audit_ontology.cli passes the flags through.
- audit_ontology.formatter renders the three failure modes per §5.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from audit_ontology import git_helpers
from audit_ontology.audit import (
    AuditReport,
    ConstraintReport,
    Summary,
    run_audit,
)
from audit_ontology.cli import main as cli_main
from audit_ontology.formatter import format_text
from audit_ontology.freshness import (
    FreshnessIssue,
    FreshnessMode,
    _env_matches,
    check_claim_freshness,
)
from iomoments_ontology import (
    DomainConstraint,
    EnvironmentSpec,
    Ontology,
    OntologyDAG,
    TestResult,
    TestResultsDAG,
    TestResultsSnapshot,
    save_snapshot,
    save_test_results_snapshot,
    save_test_results_dag,
)


# --- git fixture helpers -----------------------------------------------


def _init_repo(path: Path) -> None:
    """Initialize a real git repo for git_helpers tests."""
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


def _commit(repo: Path, file: str, content: str, msg: str) -> str:
    target = repo / file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "add", file], cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=repo, check=True, capture_output=True,
    )
    rc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return rc.stdout.strip()


# --- git_helpers --------------------------------------------------------


def test_head_sha_returns_full_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "f.txt", "x", "first")
    assert git_helpers.head_sha(tmp_path) == sha


def test_head_sha_returns_none_outside_git(tmp_path: Path) -> None:
    assert git_helpers.head_sha(tmp_path) is None


def test_last_touch_file_returns_latest_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "v1", "first a")
    _commit(tmp_path, "b.txt", "v1", "first b")
    sha3 = _commit(tmp_path, "a.txt", "v2", "second a")
    assert git_helpers.last_touch_file(tmp_path, "a.txt") == sha3
    assert git_helpers.last_touch_file(tmp_path, "a.txt") != sha1


def test_last_touch_file_returns_none_for_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "v1", "first")
    assert git_helpers.last_touch_file(tmp_path, "never_existed.txt") is None


def test_is_ancestor_of_head_for_real_ancestor(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "v1", "first")
    _commit(tmp_path, "a.txt", "v2", "second")
    assert git_helpers.is_ancestor_of_head(tmp_path, sha1) is True


def test_is_ancestor_of_head_for_unrelated_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "v1", "first")
    assert git_helpers.is_ancestor_of_head(tmp_path, "0" * 40) is False


def test_is_at_or_after_equal_returns_true(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "a.txt", "v1", "first")
    assert git_helpers.is_at_or_after(tmp_path, sha, sha) is True


def test_is_at_or_after_strictly_later(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "v1", "first")
    sha2 = _commit(tmp_path, "a.txt", "v2", "second")
    assert git_helpers.is_at_or_after(tmp_path, sha2, sha1) is True
    assert git_helpers.is_at_or_after(tmp_path, sha1, sha2) is False


# --- _env_matches structural subtyping ---------------------------------


def test_env_matches_empty_expected_wildcards() -> None:
    """Empty fields on expected env match any value on actual."""
    actual = EnvironmentSpec(
        kind="vmtest", kernel="v6.18", distro="fedora",
    )
    expected = EnvironmentSpec(kind="vmtest")
    assert _env_matches(actual, expected) is True


def test_env_matches_non_empty_must_equal() -> None:
    actual = EnvironmentSpec(kind="vmtest", kernel="v6.18")
    expected = EnvironmentSpec(kind="vmtest", kernel="v5.15")
    assert _env_matches(actual, expected) is False


def test_env_matches_kind_mismatch() -> None:
    actual = EnvironmentSpec(kind="host")
    expected = EnvironmentSpec(kind="vmtest")
    assert _env_matches(actual, expected) is False


def test_env_matches_required_flag() -> None:
    actual = EnvironmentSpec(kind="host", flags={"perf": "1"})
    expected = EnvironmentSpec(kind="host", flags={"perf": "1"})
    assert _env_matches(actual, expected) is True


def test_env_matches_missing_required_flag() -> None:
    actual = EnvironmentSpec(kind="host")
    expected = EnvironmentSpec(kind="host", flags={"perf": "1"})
    assert _env_matches(actual, expected) is False


# --- check_claim_freshness ---------------------------------------------


def _ts() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def test_freshness_skips_non_tested_status(tmp_path: Path) -> None:
    """Claim with status='spec' yields no freshness issues regardless."""
    _init_repo(tmp_path)
    _commit(tmp_path, "src.py", "def x(): pass", "first")
    claim = DomainConstraint(
        name="c", description="d", status="spec",
        verification_refs=["tests/x.py::test_x"],
    )
    issues = check_claim_freshness(
        "domain", claim, TestResultsSnapshot(), tmp_path,
    )
    assert not issues


def test_freshness_env_never_exercised(tmp_path: Path) -> None:
    """No TestResult exists for the verification_ref → ENV_NEVER_EXERCISED."""
    _init_repo(tmp_path)
    _commit(tmp_path, "tests/x.py", "def test_x(): pass", "first")
    claim = DomainConstraint(
        name="c", description="d", status="tested",
        verification_refs=["tests/x.py::test_x"],
    )
    issues = check_claim_freshness(
        "domain", claim, TestResultsSnapshot(), tmp_path,
    )
    assert len(issues) == 1
    assert issues[0].mode is FreshnessMode.ENV_NEVER_EXERCISED


def test_freshness_runner_forgot_when_other_env_exists(
    tmp_path: Path,
) -> None:
    """Result exists for the ref in a different env → RUNNER_FORGOT
    (the producer fires, just not in the required env)."""
    _init_repo(tmp_path)
    _commit(tmp_path, "tests/x.py", "def test_x(): pass", "first")
    claim = DomainConstraint(
        name="c", description="d", status="tested",
        verification_refs=["tests/x.py::test_x"],
        expected_environments=[
            EnvironmentSpec(kind="vmtest", kernel="v5.15"),
        ],
    )
    snapshot = TestResultsSnapshot(results=[
        TestResult(
            verification_ref="tests/x.py::test_x",
            environment=EnvironmentSpec(kind="host"),
            outcome="pass",
            captured_git_sha=git_helpers.head_sha(tmp_path) or "a" * 40,
            captured_at=_ts(),
        ),
    ])
    issues = check_claim_freshness(
        "domain", claim, snapshot, tmp_path,
    )
    assert len(issues) == 1
    assert issues[0].mode is FreshnessMode.RUNNER_FORGOT


def test_freshness_stale_result(tmp_path: Path) -> None:
    """Result captured at an old SHA, code edited since → STALE_RESULT."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "tests/x.py", "def test_x(): pass", "first")
    # Edit the file; new SHA > sha1.
    _commit(
        tmp_path, "tests/x.py", "def test_x(): pass  # edit", "second",
    )
    claim = DomainConstraint(
        name="c", description="d", status="tested",
        verification_refs=["tests/x.py::test_x"],
    )
    # Result captured BEFORE the edit.
    snapshot = TestResultsSnapshot(results=[
        TestResult(
            verification_ref="tests/x.py::test_x",
            environment=EnvironmentSpec(kind="host"),
            outcome="pass",
            captured_git_sha=sha1,
            captured_at=_ts(),
        ),
    ])
    issues = check_claim_freshness(
        "domain", claim, snapshot, tmp_path,
    )
    assert len(issues) == 1
    assert issues[0].mode is FreshnessMode.STALE_RESULT


def test_freshness_happy_path_no_issues(tmp_path: Path) -> None:
    """Result captured at HEAD → no gap."""
    _init_repo(tmp_path)
    _commit(tmp_path, "tests/x.py", "def test_x(): pass", "first")
    head = git_helpers.head_sha(tmp_path)
    assert head is not None
    claim = DomainConstraint(
        name="c", description="d", status="tested",
        verification_refs=["tests/x.py::test_x"],
    )
    snapshot = TestResultsSnapshot(results=[
        TestResult(
            verification_ref="tests/x.py::test_x",
            environment=EnvironmentSpec(kind="host"),
            outcome="pass",
            captured_git_sha=head,
            captured_at=_ts(),
        ),
    ])
    issues = check_claim_freshness(
        "domain", claim, snapshot, tmp_path,
    )
    assert not issues


# --- run_audit integration ---------------------------------------------


def _build_audit_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build (ontology DAG path, test-results DAG path, repo_root).

    Repo has one tested claim; test-results DAG seeded with a fresh
    pass for it.
    """
    _init_repo(tmp_path)
    _commit(
        tmp_path, "tests/x.py", "def test_x(): pass\n",
        "add test",
    )
    head = git_helpers.head_sha(tmp_path)
    assert head is not None

    onto_dag_path = tmp_path / "ontology.json"
    onto_dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        onto_dag,
        Ontology(domain_constraints=[
            DomainConstraint(
                name="c1", description="d", status="tested",
                verification_refs=["tests/x.py::test_x"],
            ),
        ]),
        label="seed",
    )
    onto_dag_path.write_text(onto_dag.to_json(), encoding="utf-8")

    tr_dag_path = tmp_path / "test-results.json"
    tr_dag = TestResultsDAG(project_name="iomoments")
    save_test_results_snapshot(
        tr_dag,
        TestResultsSnapshot(results=[
            TestResult(
                verification_ref="tests/x.py::test_x",
                environment=EnvironmentSpec(kind="host"),
                outcome="pass",
                captured_git_sha=head,
                captured_at=_ts(),
            ),
        ]),
        label="seed",
    )
    save_test_results_dag(tr_dag, str(tr_dag_path))

    return onto_dag_path, tr_dag_path, tmp_path


def test_run_audit_freshness_default_off_no_check(tmp_path: Path) -> None:
    """Without --enforce-freshness, the freshness pass doesn't run."""
    onto_dag_path, _, repo = _build_audit_fixture(tmp_path)
    report = run_audit(onto_dag_path, repo)
    assert report.summary.freshness_gaps == 0
    assert not report.freshness_issues


def test_run_audit_freshness_enabled_clean(tmp_path: Path) -> None:
    """With --enforce-freshness, fresh fixture passes cleanly."""
    onto_dag_path, tr_dag_path, repo = _build_audit_fixture(tmp_path)
    report = run_audit(
        onto_dag_path, repo,
        test_results_dag_path=tr_dag_path,
        enforce_freshness=True,
    )
    assert report.summary.freshness_gaps == 0


def test_run_audit_freshness_enabled_finds_gap(tmp_path: Path) -> None:
    """With --enforce-freshness and no test-results DAG, every claim
    surfaces an ENV_NEVER_EXERCISED gap."""
    onto_dag_path, _, repo = _build_audit_fixture(tmp_path)
    report = run_audit(
        onto_dag_path, repo,
        test_results_dag_path=None,
        enforce_freshness=True,
    )
    assert report.summary.freshness_gaps == 1
    assert len(report.freshness_issues) == 1
    assert (
        report.freshness_issues[0].mode
        is FreshnessMode.ENV_NEVER_EXERCISED
    )


def test_run_audit_bootstrap_downgrades_missing_result(
    tmp_path: Path,
) -> None:
    """Bootstrap mode: missing-result and never-exercised → not gaps,
    but still recorded in freshness_issues for visibility."""
    onto_dag_path, _, repo = _build_audit_fixture(tmp_path)
    report = run_audit(
        onto_dag_path, repo,
        test_results_dag_path=None,
        enforce_freshness=True,
        bootstrap=True,
    )
    assert report.summary.freshness_gaps == 0
    # Issue is still recorded (visibility), just not gating.
    assert len(report.freshness_issues) == 1


def test_run_audit_bootstrap_does_not_downgrade_stale_result(
    tmp_path: Path,
) -> None:
    """STALE_RESULT is a real freshness regression — bootstrap does
    NOT downgrade it (the producer fired, then code moved past it,
    that's a genuine signal)."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "tests/x.py", "v1", "first")
    _commit(tmp_path, "tests/x.py", "v2", "second")  # edit

    onto_dag_path = tmp_path / "ontology.json"
    onto_dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        onto_dag,
        Ontology(domain_constraints=[
            DomainConstraint(
                name="c1", description="d", status="tested",
                verification_refs=["tests/x.py::test_x"],
            ),
        ]),
        label="seed",
    )
    onto_dag_path.write_text(onto_dag.to_json(), encoding="utf-8")

    tr_dag_path = tmp_path / "test-results.json"
    tr_dag = TestResultsDAG(project_name="iomoments")
    save_test_results_snapshot(
        tr_dag,
        TestResultsSnapshot(results=[
            TestResult(
                verification_ref="tests/x.py::test_x",
                environment=EnvironmentSpec(kind="host"),
                outcome="pass",
                captured_git_sha=sha1,  # OLD sha
                captured_at=_ts(),
            ),
        ]),
        label="seed",
    )
    save_test_results_dag(tr_dag, str(tr_dag_path))

    report = run_audit(
        onto_dag_path, tmp_path,
        test_results_dag_path=tr_dag_path,
        enforce_freshness=True,
        bootstrap=True,  # bootstrap should NOT save us
    )
    assert report.summary.freshness_gaps == 1, (
        "STALE_RESULT must continue to gate even in bootstrap mode"
    )


# --- CLI ---------------------------------------------------------------


def test_cli_default_no_freshness(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare invocation does not run the freshness pass."""
    onto_dag_path, _, repo = _build_audit_fixture(tmp_path)
    rc = cli_main(["--dag", str(onto_dag_path), "--repo-root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "freshness gaps        : 0" in out


def test_cli_enforce_freshness_no_results_gates(
    tmp_path: Path
) -> None:
    """--enforce-freshness without producers → exit non-zero on gap."""
    onto_dag_path, _, repo = _build_audit_fixture(tmp_path)
    # Point at a non-existent test-results DAG.
    fake_tr = tmp_path / "no-such.json"
    rc = cli_main([
        "--dag", str(onto_dag_path),
        "--repo-root", str(repo),
        "--test-results-dag", str(fake_tr),
        "--enforce-freshness",
        "--exit-nonzero-on-gap",
    ])
    assert rc == 1


def test_cli_bootstrap_lets_missing_results_through(
    tmp_path: Path
) -> None:
    """--bootstrap downgrades never-exercised so the gate exits 0."""
    onto_dag_path, _, repo = _build_audit_fixture(tmp_path)
    fake_tr = tmp_path / "no-such.json"
    rc = cli_main([
        "--dag", str(onto_dag_path),
        "--repo-root", str(repo),
        "--test-results-dag", str(fake_tr),
        "--enforce-freshness",
        "--bootstrap",
        "--exit-nonzero-on-gap",
    ])
    assert rc == 0


# --- formatter ---------------------------------------------------------


def test_formatter_renders_runner_forgot() -> None:
    issue = FreshnessIssue(
        claim_kind="domain", claim_name="x",
        verification_ref="tests/x.py::test_x",
        environment=EnvironmentSpec(kind="host"),
        mode=FreshnessMode.RUNNER_FORGOT,
        reason="src.py last edited at abc12345; no test result captured",
        fix_recipe=".venv/bin/pytest tests/x.py::test_x",
    )
    report = AuditReport(
        rows=[ConstraintReport(
            kind="domain", name="x", status="tested",
            implementation=[], verification=[],
            consistency_violations=[],
        )],
        summary=Summary(total_rows=1),
        freshness_issues=[issue],
    )
    text = format_text(report)
    assert "runner forgot to fire a test" in text
    assert "tests/x.py::test_x" in text
    assert ".venv/bin/pytest tests/x.py::test_x" in text


def test_formatter_renders_stale_result() -> None:
    issue = FreshnessIssue(
        claim_kind="domain", claim_name="x",
        verification_ref="tests/x.py::test_x",
        environment=EnvironmentSpec(kind="vmtest", kernel="v6.18"),
        mode=FreshnessMode.STALE_RESULT,
        reason="latest result captured at abc precedes last edit at def",
        fix_recipe="",
    )
    report = AuditReport(
        rows=[ConstraintReport(
            kind="domain", name="x", status="tested",
            implementation=[], verification=[],
            consistency_violations=[],
        )],
        summary=Summary(total_rows=1),
        freshness_issues=[issue],
    )
    text = format_text(report)
    assert "stale result" in text
    assert "kernel='v6.18'" in text
    # No fix_recipe → no "fix:" line
    assert "fix:" not in text


def test_formatter_renders_env_never_exercised() -> None:
    issue = FreshnessIssue(
        claim_kind="domain", claim_name="x",
        verification_ref="tests/x.py::test_x",
        environment=EnvironmentSpec(
            kind="aws-ec2", distro="ubuntu-20.04",
        ),
        mode=FreshnessMode.ENV_NEVER_EXERCISED,
        reason="no passing TestResult for verification_ref in this env",
        fix_recipe="bash scripts/aws_tracer.sh",
    )
    report = AuditReport(
        rows=[ConstraintReport(
            kind="domain", name="x", status="tested",
            implementation=[], verification=[],
            consistency_violations=[],
        )],
        summary=Summary(total_rows=1),
        freshness_issues=[issue],
    )
    text = format_text(report)
    assert "environment never exercised" in text
    assert "distro='ubuntu-20.04'" in text
    assert "bash scripts/aws_tracer.sh" in text
