"""Phase 6 tests (D009): audit-ontology package.

Layered coverage:
- parser.parse_ref: empty, whitespace, no-symbol, ``:`` and ``::``
  forms, edge cases.
- resolver.resolve_ref: file missing, symbol missing, file + symbol
  OK per language, non-UTF-8 file, unknown extension falls back to
  word-boundary substring match.
- consistency.check_status_refs_consistency: all five status values.
- audit.run_audit end-to-end on the shipped DAG and on a synthetic
  DAG with mixed gap patterns.
- formatter.format_text: empty ontology, happy path, gap-full path.
- cli.main: bare invocation, --exit-nonzero-on-gap under both
  outcomes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_ontology import (
    AuditReport,
    Resolution,
    check_status_refs_consistency,
    format_text,
    parse_ref,
    resolve_ref,
    run_audit,
)
from audit_ontology.cli import main as cli_main
from audit_ontology.consistency import ConstraintFields
from iomoments_ontology import (
    DomainConstraint,
    Ontology,
    OntologyDAG,
    PerformanceConstraint,
    save_snapshot,
)
from iomoments_ontology.build import build_ontology_from_yaml


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_DAG = _REPO_ROOT / "tooling" / "iomoments-ontology.json"
_SHIPPED_YAML = _REPO_ROOT / "tooling" / "iomoments-ontology.yaml"


# --- parser -------------------------------------------------------------


def test_parse_ref_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_ref("")
    with pytest.raises(ValueError):
        parse_ref("   ")


def test_parse_ref_file_only() -> None:
    r = parse_ref("src/iomoments.h")
    assert r.path == "src/iomoments.h"
    assert r.symbol is None


def test_parse_ref_colon_style() -> None:
    r = parse_ref("src/iomoments.c:pebay_update")
    assert r.path == "src/iomoments.c"
    assert r.symbol == "pebay_update"


def test_parse_ref_double_colon_style() -> None:
    r = parse_ref("tests/test_pebay_ref.py::test_round_trip")
    assert r.path == "tests/test_pebay_ref.py"
    assert r.symbol == "test_round_trip"


def test_parse_ref_strips_whitespace() -> None:
    r = parse_ref("  src/iomoments.c : pebay_update  ")
    assert r.path == "src/iomoments.c"
    assert r.symbol == "pebay_update"


def test_parse_ref_preserves_raw() -> None:
    """Raw input is preserved verbatim for error messages."""
    r = parse_ref("src/foo.c:bar")
    assert r.raw == "src/foo.c:bar"


def test_parse_ref_rejects_empty_symbol() -> None:
    """``foo.py::`` is a typo, not a file-only ref — surface it."""
    with pytest.raises(ValueError, match="empty symbol"):
        parse_ref("foo.py::")
    with pytest.raises(ValueError, match="empty symbol"):
        parse_ref("foo.py:")


def test_parse_ref_rejects_empty_path() -> None:
    """``::test_x`` has nothing to resolve against."""
    with pytest.raises(ValueError, match="empty path"):
        parse_ref("::test_x")
    with pytest.raises(ValueError, match="empty path"):
        parse_ref(":test_x")


# --- resolver -----------------------------------------------------------


def test_resolver_file_missing(tmp_path: Path) -> None:
    r = parse_ref("nope.py:foo")
    result = resolve_ref(r, tmp_path)
    assert result.resolution is Resolution.FILE_MISSING


def test_resolver_python_def_found(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "def target_func():\n    return 42\n",
        encoding="utf-8",
    )
    r = parse_ref("sample.py:target_func")
    result = resolve_ref(r, tmp_path)
    assert result.resolution is Resolution.OK
    assert result.line == 1


def test_resolver_python_class_found(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "\n\nclass Thing:\n    pass\n",
        encoding="utf-8",
    )
    result = resolve_ref(parse_ref("mod.py:Thing"), tmp_path)
    assert result.resolution is Resolution.OK
    assert result.line == 3


def test_resolver_python_symbol_missing(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    result = resolve_ref(parse_ref("mod.py:nonexistent"), tmp_path)
    assert result.resolution is Resolution.SYMBOL_MISSING


def test_resolver_c_function_found(tmp_path: Path) -> None:
    (tmp_path / "hot.c").write_text(
        "static int pebay_update(int n) {\n    return n + 1;\n}\n",
        encoding="utf-8",
    )
    result = resolve_ref(parse_ref("hot.c:pebay_update"), tmp_path)
    assert result.resolution is Resolution.OK


def test_resolver_bpf_sec_macro_found(tmp_path: Path) -> None:
    """libbpf SEC("kprobe/...") + function header matches the BPF pattern."""
    (tmp_path / "hot.bpf.c").write_text(
        'SEC("kprobe/blk_mq_start_request")\n'
        "int probe_fn(struct pt_regs *ctx) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    result = resolve_ref(parse_ref("hot.bpf.c:probe_fn"), tmp_path)
    # The file suffix is .c so C patterns apply; we grep the C
    # function-definition form which catches `int probe_fn(`.
    assert result.resolution is Resolution.OK


def test_resolver_unknown_extension_falls_back(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text(
        "Everyone loves moments_are_finite tests.\n",
        encoding="utf-8",
    )
    r = parse_ref("notes.md:moments_are_finite")
    result = resolve_ref(r, tmp_path)
    assert result.resolution is Resolution.OK


def test_resolver_non_utf8_file(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe binary payload")
    # Save as .py so the resolver tries to grep it.
    dst = tmp_path / "binary.py"
    dst.write_bytes(b"\xff\xfe binary payload")
    result = resolve_ref(parse_ref("binary.py:anything"), tmp_path)
    assert result.resolution is Resolution.SYMBOL_MISSING
    assert "UTF-8" in result.notes


def test_resolver_file_only_ref_succeeds_on_existing_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "present.py").write_text("", encoding="utf-8")
    result = resolve_ref(parse_ref("present.py"), tmp_path)
    assert result.resolution is Resolution.OK
    assert result.line is None


def test_resolver_directory_reports_file_missing(tmp_path: Path) -> None:
    """A ref whose path is a directory isn't a file — FILE_MISSING."""
    (tmp_path / "subdir").mkdir()
    result = resolve_ref(parse_ref("subdir"), tmp_path)
    assert result.resolution is Resolution.FILE_MISSING


def test_resolver_handles_regex_metachars_in_symbol(
    tmp_path: Path,
) -> None:
    """Unknown extension + symbol with regex metacharacters doesn't crash.

    re.escape inside _compiled makes the pattern safe. This pins
    that invariant so a future pattern rewrite can't regress it.
    """
    (tmp_path / "notes.md").write_text(
        "foo.bar is referenced here.\n",
        encoding="utf-8",
    )
    result = resolve_ref(parse_ref("notes.md:foo.bar"), tmp_path)
    assert result.resolution is Resolution.OK


# --- consistency --------------------------------------------------------


def _fields(
    status: str,
    impl: list[str] | None = None,
    verif: list[str] | None = None,
    rationale: str = "",
) -> ConstraintFields:
    return ConstraintFields(
        name="cons",
        status=status,
        rationale=rationale,
        implementation_refs=list(impl or []),
        verification_refs=list(verif or []),
    )


def test_consistency_spec_is_always_ok() -> None:
    assert not check_status_refs_consistency(_fields("spec"))


def test_consistency_n_a_is_always_ok() -> None:
    assert not check_status_refs_consistency(_fields("n_a"))


def test_consistency_tested_requires_verification_refs() -> None:
    viols = check_status_refs_consistency(_fields("tested"))
    assert any("verification_refs empty" in v for v in viols)


def test_consistency_tested_ok_with_verification() -> None:
    viols = check_status_refs_consistency(
        _fields("tested", verif=["tests/foo.py::test_bar"]),
    )
    assert not viols


def test_consistency_implemented_requires_both() -> None:
    viols = check_status_refs_consistency(_fields("implemented"))
    assert len(viols) == 2


def test_consistency_implemented_missing_only_impl() -> None:
    viols = check_status_refs_consistency(
        _fields("implemented", verif=["tests/foo.py::test_bar"]),
    )
    assert len(viols) == 1
    assert "implementation_refs empty" in viols[0]


def test_consistency_implemented_missing_only_verif() -> None:
    viols = check_status_refs_consistency(
        _fields("implemented", impl=["src/foo.c:bar"]),
    )
    assert len(viols) == 1
    assert "verification_refs empty" in viols[0]


def test_consistency_deviation_requires_rationale() -> None:
    viols = check_status_refs_consistency(_fields("deviation"))
    assert any("rationale empty" in v for v in viols)


def test_consistency_deviation_with_rationale_ok() -> None:
    viols = check_status_refs_consistency(
        _fields("deviation", rationale="See D042 — known deviation."),
    )
    assert not viols


# --- run_audit end-to-end ----------------------------------------------


def test_run_audit_empty_dag(tmp_path: Path) -> None:
    """Empty DAG yields an empty report (not a crash)."""
    dag_path = tmp_path / "dag.json"
    report = run_audit(dag_path, tmp_path)
    assert not report.rows
    assert report.summary.total_rows == 0
    assert report.has_any_gap is False


def test_run_audit_clean_ontology(tmp_path: Path) -> None:
    """All-spec ontology produces zero gaps."""
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag,
        Ontology(domain_constraints=[
            DomainConstraint(name="c1", description="d1"),
        ]),
        label="clean",
    )
    dag_path.write_text(dag.to_json(), encoding="utf-8")
    report = run_audit(dag_path, tmp_path)
    assert report.summary.rows_with_gap == 0


def test_run_audit_flags_missing_refs(tmp_path: Path) -> None:
    """tested-status with a bogus verification_ref surfaces as a gap."""
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag,
        Ontology(domain_constraints=[
            DomainConstraint(
                name="bogus_ref",
                description="cites a file that doesn't exist",
                status="tested",
                verification_refs=["nope/nonexistent.py::test_x"],
            ),
        ]),
        label="gap",
    )
    dag_path.write_text(dag.to_json(), encoding="utf-8")
    report = run_audit(dag_path, tmp_path)
    assert report.summary.rows_with_gap == 1
    assert report.summary.refs_file_missing == 1


def test_run_audit_flags_consistency_violation(tmp_path: Path) -> None:
    """status='implemented' with empty refs yields consistency violations."""
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag,
        Ontology(performance_constraints=[
            PerformanceConstraint(
                name="bogus_perf",
                description="claims implemented with no evidence",
                metric="m",
                budget=1.0,
                unit="ns",
                direction="max",
                status="implemented",
            ),
        ]),
        label="gap",
    )
    dag_path.write_text(dag.to_json(), encoding="utf-8")
    report = run_audit(dag_path, tmp_path)
    assert report.summary.consistency_violations == 2  # both refs empty
    assert report.summary.rows_with_gap == 1


def test_run_audit_on_shipped_dag() -> None:
    """The shipped ontology audits clean — refs resolve, no consistency gaps.

    As code lands and constraints get implementation_refs /
    verification_refs populated, the resolver is the load-bearing
    check: refs_file_missing + refs_symbol_missing must stay at 0 and
    rows_with_gap must stay at 0. refs_total naturally grows with the
    codebase, so we don't pin it to a specific number.
    """
    if not _SHIPPED_DAG.exists() or not _SHIPPED_YAML.exists():
        pytest.skip("shipped artifacts missing")
    report = run_audit(_SHIPPED_DAG, _REPO_ROOT)
    assert report.summary.refs_file_missing == 0
    assert report.summary.refs_symbol_missing == 0
    assert report.summary.rows_with_gap == 0
    assert report.summary.consistency_violations == 0


# --- formatter ----------------------------------------------------------


def test_format_text_empty() -> None:
    text = format_text(AuditReport())
    assert "empty" in text.lower()


def test_format_text_includes_summary(tmp_path: Path) -> None:
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag,
        Ontology(domain_constraints=[
            DomainConstraint(name="c1", description="d1"),
        ]),
        label="for-format",
    )
    dag_path.write_text(dag.to_json(), encoding="utf-8")
    report = run_audit(dag_path, tmp_path)
    out = format_text(report)
    assert "summary" in out
    assert "total rows" in out
    assert "[domain] c1" in out


def test_format_text_marks_gaps(tmp_path: Path) -> None:
    """Gap rows print 'GAP' rather than 'ok'."""
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag,
        Ontology(domain_constraints=[
            DomainConstraint(
                name="gappy",
                description="missing impl",
                status="implemented",
            ),
        ]),
        label="gap",
    )
    dag_path.write_text(dag.to_json(), encoding="utf-8")
    report = run_audit(dag_path, tmp_path)
    out = format_text(report)
    assert "GAP" in out
    assert "implementation_refs empty" in out


# --- CLI ---------------------------------------------------------------


def test_cli_bare_invocation_exits_zero_on_gap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --exit-nonzero-on-gap, gaps still exit 0."""
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag,
        Ontology(domain_constraints=[
            DomainConstraint(
                name="cli_gap",
                description="tested with no verif refs",
                status="tested",
            ),
        ]),
        label="cli-gap",
    )
    dag_path.write_text(dag.to_json(), encoding="utf-8")

    rc = cli_main([
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
    ])
    assert rc == 0
    assert "GAP" in capsys.readouterr().out


def test_cli_exit_nonzero_on_gap_fires(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --exit-nonzero-on-gap, gaps yield rc=1."""
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag,
        Ontology(domain_constraints=[
            DomainConstraint(
                name="cli_gap",
                description="tested with no verif refs",
                status="tested",
            ),
        ]),
        label="cli-gap",
    )
    dag_path.write_text(dag.to_json(), encoding="utf-8")

    rc = cli_main([
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
        "--exit-nonzero-on-gap",
    ])
    assert rc == 1
    assert "GAP" in capsys.readouterr().out


def test_cli_exit_nonzero_on_gap_clean_is_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Clean ontology + --exit-nonzero-on-gap yields rc=0."""
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(
        dag,
        Ontology(),
        label="clean",
    )
    dag_path.write_text(dag.to_json(), encoding="utf-8")

    rc = cli_main([
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
        "--exit-nonzero-on-gap",
    ])
    assert rc == 0
    capsys.readouterr()


def test_cli_without_argv_uses_sys_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cli_main() with no argument falls back to sys.argv[1:]."""
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(dag, Ontology(), label="sysargv")
    dag_path.write_text(dag.to_json(), encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["audit-ontology", "--dag", str(dag_path),
         "--repo-root", str(tmp_path)],
    )
    assert cli_main() == 0
    capsys.readouterr()


def test_cli_returns_rc_2_on_malformed_dag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Tooling error (bad JSON) yields rc=2, distinct from gap (rc=1)."""
    dag_path = tmp_path / "dag.json"
    dag_path.write_text("{not-json", encoding="utf-8")
    rc = cli_main([
        "--dag", str(dag_path),
        "--repo-root", str(tmp_path),
        "--exit-nonzero-on-gap",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "tooling error" in err


# --- Shipped ontology sanity -------------------------------------------


def test_shipped_ontology_matches_yaml_source() -> None:
    """Shipped DAG's current node reflects the shipped YAML, and the
    audit summary on that DAG matches what we expect for the current
    draft-first state (all status=spec, no refs)."""
    if not _SHIPPED_DAG.exists() or not _SHIPPED_YAML.exists():
        pytest.skip("shipped artifacts missing")
    report = run_audit(_SHIPPED_DAG, _REPO_ROOT)
    # Count rows across the four auditable kinds directly from YAML.
    ont = build_ontology_from_yaml(_SHIPPED_YAML)
    expected = (
        len(ont.domain_constraints)
        + len(ont.performance_constraints)
        + len(ont.diagnostic_signals)
        + len(ont.verdict_nodes)
    )
    assert report.summary.total_rows == expected
