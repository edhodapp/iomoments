"""Adversarial fixture suite — proves the audit catches each documented
negative case. Audit-of-audit pattern (D013 recursive-primitive applied
to the project's own discipline tools).

Each fixture constructs a synthetic ontology + working tree designed to
trigger one specific failure mode, runs the audit end-to-end, and
asserts the failure is detected. If the audit ever passes one of these
fixtures cleanly, something has regressed.

Distinct from test_audit_ontology.py, which unit-tests parser /
resolver / consistency / formatter / cli individually. This file
proves the integration: each forbidden combination, when realized
against a real DAG + working tree, is caught.
"""

from __future__ import annotations

from pathlib import Path

from audit_ontology import Resolution, run_audit
from iomoments_ontology import (
    DiagnosticSignal,
    DomainConstraint,
    Ontology,
    OntologyDAG,
    PerformanceConstraint,
    VerdictNode,
    save_snapshot,
)

# The four constraint kinds the audit must iterate. Pinned as a
# constant so tests can assert set equality rather than subset
# containment — a regression that drops a kind silently from
# audit._build_row's sources tuple breaks every test that uses this.
_AUDITED_KINDS = {"domain", "perf", "signal", "verdict"}


def _build_dag(tmp_path: Path, ontology: Ontology) -> Path:
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(dag, ontology, label="fixture")
    dag_path.write_text(dag.to_json(), encoding="utf-8")
    return dag_path


# --- Status/refs consistency violations --------------------------------


def test_negative_tested_with_empty_verif_refs(tmp_path: Path) -> None:
    """status='tested' with verification_refs=[] must be flagged."""
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(name="c", description="d", status="tested"),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.consistency_violations == 1
    assert report.summary.rows_with_gap == 1
    assert any(
        "verification_refs empty" in v
        for v in report.rows[0].consistency_violations
    )


def test_negative_implemented_missing_impl_refs(tmp_path: Path) -> None:
    """status='implemented' demands BOTH refs lists; missing impl caught."""
    (tmp_path / "src.py").write_text(
        "def x():\n    pass\n", encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            verification_refs=["src.py::x"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.consistency_violations == 1
    assert report.summary.rows_with_gap == 1
    assert any(
        "implementation_refs empty" in v
        for v in report.rows[0].consistency_violations
    )


def test_negative_implemented_missing_verif_refs(tmp_path: Path) -> None:
    """status='implemented' with impl_refs but no verif_refs is caught."""
    (tmp_path / "src.py").write_text(
        "def x():\n    pass\n", encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            implementation_refs=["src.py::x"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.consistency_violations == 1
    assert report.summary.rows_with_gap == 1
    assert any(
        "verification_refs empty" in v
        for v in report.rows[0].consistency_violations
    )


def test_negative_deviation_without_rationale(tmp_path: Path) -> None:
    """status='deviation' with empty rationale must be flagged."""
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(name="c", description="d", status="deviation"),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.consistency_violations == 1
    assert report.summary.rows_with_gap == 1
    assert any(
        "rationale empty" in v
        for v in report.rows[0].consistency_violations
    )


# --- Resolution failures -----------------------------------------------


def test_negative_verif_ref_file_missing(tmp_path: Path) -> None:
    """verification_ref pointing at a file that doesn't exist."""
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="tested",
            verification_refs=["never/existed.py::test"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.refs_file_missing == 1
    assert report.summary.refs_symbol_missing == 0
    assert report.summary.consistency_violations == 0
    assert report.summary.rows_with_gap == 1


def test_negative_impl_ref_file_missing(tmp_path: Path) -> None:
    """implementation_ref pointing at a file that doesn't exist."""
    (tmp_path / "test.py").write_text(
        "def t():\n    pass\n", encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            implementation_refs=["never/existed.c:func"],
            verification_refs=["test.py::t"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.refs_file_missing == 1
    assert report.summary.refs_symbol_missing == 0
    assert report.summary.consistency_violations == 0
    assert report.summary.rows_with_gap == 1


def test_negative_symbol_absent_from_real_file(tmp_path: Path) -> None:
    """File exists but doesn't define the named symbol anywhere."""
    (tmp_path / "src.py").write_text(
        "# empty module\n", encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="tested",
            verification_refs=["src.py::absent_symbol"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.refs_symbol_missing == 1
    assert report.summary.refs_file_missing == 0
    assert report.summary.consistency_violations == 0
    assert report.summary.rows_with_gap == 1


def test_negative_symbol_only_in_python_comment(tmp_path: Path) -> None:
    """Decoy: symbol name appears in a comment but is never defined.

    Paired with a positive control in the same fixture: a real
    ``def real_func(...):`` in a sibling file. The audit must reject
    the comment-only decoy AND accept the real def — both halves are
    required to prove the resolver discriminates rather than always
    rejecting (or always accepting) ``.py`` symbols.
    """
    (tmp_path / "decoy.py").write_text(
        "# def my_func(a, b):\n"
        "#     return a + b\n",
        encoding="utf-8",
    )
    (tmp_path / "real.py").write_text(
        "def real_func(x):\n    return x\n",
        encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            implementation_refs=["real.py::real_func"],
            verification_refs=["decoy.py::my_func"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    decoy_resolution = report.rows[0].verification[0].resolution
    real_resolution = report.rows[0].implementation[0].resolution
    assert decoy_resolution is Resolution.SYMBOL_MISSING, (
        "audit accepted a Python symbol that exists only in a "
        "comment — decoy detection regressed"
    )
    assert real_resolution is Resolution.OK, (
        "positive control failed — resolver may have regressed to "
        "always-reject for .py files"
    )


def test_negative_symbol_only_in_c_comment(tmp_path: Path) -> None:
    """Decoy: C symbol named only inside a // comment must not match.

    Paired with a positive control: a real ``void real_func(int x)
    {...}`` in a sibling file. The audit must reject the comment-only
    decoy AND accept the real def.
    """
    (tmp_path / "decoy.c").write_text(
        "// void my_func(int x) { return; }\n",
        encoding="utf-8",
    )
    (tmp_path / "real.c").write_text(
        "void real_func(int x) {\n    return;\n}\n",
        encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            implementation_refs=["real.c:real_func"],
            verification_refs=["decoy.c:my_func"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    decoy_resolution = report.rows[0].verification[0].resolution
    real_resolution = report.rows[0].implementation[0].resolution
    assert decoy_resolution is Resolution.SYMBOL_MISSING, (
        "audit accepted a C symbol that exists only in a // comment "
        "— decoy detection regressed"
    )
    assert real_resolution is Resolution.OK, (
        "positive control failed — resolver may have regressed to "
        "always-reject for .c files"
    )


def test_negative_python_substring_of_longer_identifier(
    tmp_path: Path,
) -> None:
    """Searching for ``foo`` must NOT match ``def foobar(...)``.

    Paired with a positive control: ``def foo(...)`` in a sibling
    file. The audit must reject the substring-match while accepting
    the exact-match, proving the regex anchors are pinning the name.
    """
    (tmp_path / "decoy.py").write_text(
        "def foobar(x):\n    return x\n", encoding="utf-8",
    )
    (tmp_path / "real.py").write_text(
        "def foo(x):\n    return x\n", encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            implementation_refs=["real.py::foo"],
            verification_refs=["decoy.py::foo"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    decoy_resolution = report.rows[0].verification[0].resolution
    real_resolution = report.rows[0].implementation[0].resolution
    assert decoy_resolution is Resolution.SYMBOL_MISSING, (
        "audit matched `foo` against `def foobar` — Python substring "
        "guard regressed"
    )
    assert real_resolution is Resolution.OK


def test_negative_c_substring_of_longer_identifier(tmp_path: Path) -> None:
    """Substring guard for C: ``pebay`` must NOT match ``pebay_update(...)``.

    Paired with a positive control: a real ``void pebay(...) {}`` in
    a sibling file.
    """
    (tmp_path / "decoy.c").write_text(
        "void pebay_update(int x) {\n    return;\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "real.c").write_text(
        "void pebay(int x) {\n    return;\n}\n",
        encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            implementation_refs=["real.c:pebay"],
            verification_refs=["decoy.c:pebay"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    decoy_resolution = report.rows[0].verification[0].resolution
    real_resolution = report.rows[0].implementation[0].resolution
    assert decoy_resolution is Resolution.SYMBOL_MISSING, (
        "audit matched `pebay` against `void pebay_update(` — C "
        "substring guard regressed"
    )
    assert real_resolution is Resolution.OK


# --- Aggregation across rows / refs ------------------------------------


def test_negative_multiple_gaps_in_one_row(tmp_path: Path) -> None:
    """A single row with multiple gaps reports each one."""
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            implementation_refs=["nope1.py:x", "nope2.py:y"],
            verification_refs=["nope3.py:z"],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.refs_file_missing == 3, (
        "expected 3 file_missing across 3 nonexistent refs in one row"
    )
    assert report.summary.rows_with_gap == 1


def test_negative_iterates_all_constraint_kinds(tmp_path: Path) -> None:
    """Every audited kind from run_audit's source tuple must visit the report.

    Seeds one constraint of each kind with status='tested' + empty
    verification_refs (so each is a guaranteed gap). Asserts both that
    the visited kinds equal _AUDITED_KINDS exactly (catches a kind
    being silently dropped) and that every seeded row appears in the
    report (catches an entire kind being skipped).
    """
    ont = Ontology(
        domain_constraints=[
            DomainConstraint(
                name="dom_bad", description="d", status="tested",
            ),
        ],
        performance_constraints=[
            PerformanceConstraint(
                name="perf_bad", description="d", metric="m",
                budget=1.0, unit="ns", direction="max",
                status="tested",
            ),
        ],
        diagnostic_signals=[
            DiagnosticSignal(
                name="signal_bad", description="d", status="tested",
            ),
        ],
        verdict_nodes=[
            VerdictNode(
                kind="green", description="d", status="tested",
            ),
        ],
    )
    dag = _build_dag(tmp_path, ont)
    report = run_audit(dag, tmp_path)
    assert report.summary.total_rows == 4
    assert report.summary.rows_with_gap == 4
    kinds_seen = {row.kind for row in report.rows}
    assert kinds_seen == _AUDITED_KINDS, (
        f"audit visited {kinds_seen}, expected {_AUDITED_KINDS}; "
        "iteration in audit._build_row's sources tuple regressed"
    )


# --- Self-consistency invariants on the audit's own output -------------


def test_invariant_summary_refs_match_externally_known_ground_truth(
    tmp_path: Path,
) -> None:
    """Summary counts must match externally-known fixture composition.

    Builds a fixture with a deliberately fixed ref population:
      - 2 OK refs (real symbols in real files)
      - 2 file_missing refs (paths that don't exist)
      - 1 symbol_missing ref (file exists, symbol absent)
    Total: 5 refs.

    Then asserts the summary counters match those exact numbers, and
    independently asserts the per-row resolution counts derived from
    iterating ResolvedRefs match the same numbers. Two independent
    paths to the same ground truth — drift in either breaks the test.
    """
    (tmp_path / "real_a.py").write_text(
        "def alpha():\n    pass\n", encoding="utf-8",
    )
    (tmp_path / "real_b.py").write_text(
        "def beta():\n    pass\n", encoding="utf-8",
    )
    (tmp_path / "has_no_symbol.py").write_text(
        "# nothing here\n", encoding="utf-8",
    )
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(
            name="c", description="d", status="implemented",
            implementation_refs=[
                "real_a.py::alpha",     # OK
                "missing_one.py::x",    # file_missing
            ],
            verification_refs=[
                "real_b.py::beta",          # OK
                "missing_two.py::y",         # file_missing
                "has_no_symbol.py::absent",  # symbol_missing
            ],
        ),
    ]))
    report = run_audit(dag, tmp_path)
    s = report.summary
    # Summary counts equal externally-known fixture composition:
    assert s.refs_total == 5
    assert s.refs_file_missing == 2
    assert s.refs_symbol_missing == 1
    # Independently count per-row ResolvedRef outcomes:
    by_resolution: dict[Resolution, int] = {r: 0 for r in Resolution}
    for row in report.rows:
        for r in (*row.implementation, *row.verification):
            by_resolution[r.resolution] += 1
    assert by_resolution[Resolution.OK] == 2
    assert by_resolution[Resolution.FILE_MISSING] == 2
    assert by_resolution[Resolution.SYMBOL_MISSING] == 1
    # And the two paths must agree:
    assert by_resolution[Resolution.FILE_MISSING] == s.refs_file_missing
    assert by_resolution[Resolution.SYMBOL_MISSING] == s.refs_symbol_missing
    assert sum(by_resolution.values()) == s.refs_total


def test_invariant_rows_with_gap_le_total_rows(tmp_path: Path) -> None:
    """rows_with_gap can never exceed total_rows."""
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(name="c1", description="d", status="tested"),
        DomainConstraint(name="c2", description="d", status="tested"),
    ]))
    report = run_audit(dag, tmp_path)
    assert report.summary.rows_with_gap <= report.summary.total_rows


def test_invariant_total_rows_equals_iteration_length(
    tmp_path: Path,
) -> None:
    """summary.total_rows must equal len(report.rows).

    Catches off-by-one or skipped-kind bugs in the iteration loop.
    """
    ont = Ontology(
        domain_constraints=[
            DomainConstraint(name="d1", description="d"),
            DomainConstraint(name="d2", description="d"),
        ],
        performance_constraints=[
            PerformanceConstraint(
                name="p1", description="d", metric="m", budget=1.0,
                unit="ns", direction="max",
            ),
        ],
    )
    dag = _build_dag(tmp_path, ont)
    report = run_audit(dag, tmp_path)
    assert report.summary.total_rows == len(report.rows)
    assert report.summary.total_rows == 3


def test_invariant_clean_ontology_has_zero_gap_metrics(
    tmp_path: Path,
) -> None:
    """All-spec ontology yields zero on every gap-shaped summary field."""
    dag = _build_dag(tmp_path, Ontology(domain_constraints=[
        DomainConstraint(name="c1", description="d"),
        DomainConstraint(name="c2", description="d"),
    ]))
    report = run_audit(dag, tmp_path)
    s = report.summary
    assert s.rows_with_gap == 0
    assert s.refs_file_missing == 0
    assert s.refs_symbol_missing == 0
    assert s.consistency_violations == 0
    assert report.has_any_gap is False
