"""Combinatorial coverage of the audit's negative-case detection (PICT).

For each PICT row generated from ``audit_negatives.pict``, materializes
a synthetic ontology + working tree according to the row's parameters,
runs the audit, and asserts that the predicted failure modes match.

The predictor (``_predict_outcome``) is deliberately simpler and
structured differently from the audit's logic — it operates on the
PICT row's parameter variables and the documented audit rules without
importing resolver / consistency / audit modules. Passing a PICT row
is then a real cross-check, not the audit grading itself.

PICT is not a hard dependency: if the binary isn't found the entire
module is skipped with an explanatory reason. The hand-curated suite
in ``test_audit_negatives.py`` is the required-coverage baseline; PICT
is a strict-improvement layer on top.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, cast

import pytest

from audit_ontology import run_audit
from iomoments_ontology import (
    DomainConstraint,
    Ontology,
    OntologyDAG,
    save_snapshot,
)
from iomoments_ontology.types import RequirementStatus

_RuleFn = Callable[[dict[str, str]], int]

_PICT_MODEL = Path(__file__).parent / "audit_negatives.pict"


def _find_pict() -> str | None:
    found = shutil.which("pict")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "pict"
    if candidate.is_file():
        return str(candidate)
    return None


_PICT_BIN = _find_pict()


def _generate_cases() -> list[dict[str, str]]:
    """Invoke PICT and parse the JSON output into a list of param dicts."""
    assert _PICT_BIN is not None
    result = subprocess.run(
        [_PICT_BIN, str(_PICT_MODEL), "/f:json"],
        capture_output=True, text=True, check=True,
    )
    raw = json.loads(result.stdout)
    return [
        {entry["key"]: entry["value"] for entry in row}
        for row in raw
    ]


_CASES: list[dict[str, str]] = _generate_cases() if _PICT_BIN else []

pytestmark = pytest.mark.skipif(
    _PICT_BIN is None,
    reason="pict not found on PATH or in ~/.local/bin",
)


# --- Materializer: PICT row → synthetic fixture ------------------------


def _materialize_ref(
    role: str,
    pattern: str,
    ref_form: str,
    file_ext: str,
    tmp_path: Path,
) -> str:
    """Build the file backing a ref, return the ref string.

    ``role`` ("impl" or "verif") namespaces the file/symbol so impl
    and verif refs in the same fixture don't collide on disk.
    """
    sym_name = f"{role}_sym"
    file_name = f"{role}_file.{file_ext}"

    if pattern == "one_file_missing":
        return _format_ref(
            f"missing_{role}.{file_ext}", sym_name, ref_form,
        )

    file_path = tmp_path / file_name
    if pattern == "one_ok":
        file_path.write_text(_real_def(sym_name, file_ext), encoding="utf-8")
    elif pattern == "one_symbol_missing":
        file_path.write_text(_empty_body(file_ext), encoding="utf-8")
    elif pattern == "one_decoy":
        file_path.write_text(
            _decoy_body(sym_name, file_ext), encoding="utf-8",
        )
    else:
        raise ValueError(f"unknown pattern: {pattern}")

    return _format_ref(file_name, sym_name, ref_form)


def _format_ref(file: str, symbol: str, ref_form: str) -> str:
    if ref_form == "file_only":
        return file
    if ref_form == "colon":
        return f"{file}:{symbol}"
    if ref_form == "double_colon":
        return f"{file}::{symbol}"
    raise ValueError(f"unknown ref_form: {ref_form}")


def _real_def(symbol: str, ext: str) -> str:
    if ext == "py":
        return f"def {symbol}():\n    pass\n"
    if ext == "c":
        return f"void {symbol}(int x) {{\n    return;\n}}\n"
    raise ValueError(f"unknown ext: {ext}")


def _empty_body(ext: str) -> str:
    if ext == "py":
        return "# empty module\n"
    if ext == "c":
        return "/* empty */\n"
    raise ValueError(f"unknown ext: {ext}")


def _decoy_body(symbol: str, ext: str) -> str:
    if ext == "py":
        return f"# def {symbol}(a, b):\n#     pass\n"
    if ext == "c":
        return f"// void {symbol}(int x) {{ return; }}\n"
    raise ValueError(f"unknown ext: {ext}")


# --- Predictor: PICT row → expected audit outcome ----------------------


def _predict_resolution(case: dict[str, str]) -> tuple[int, int]:
    """Return (file_missing, symbol_missing) counts implied by the row."""
    file_missing = 0
    symbol_missing = 0
    for pattern in (case["impl_refs"], case["verif_refs"]):
        if pattern == "one_file_missing":
            file_missing += 1
        elif pattern in ("one_symbol_missing", "one_decoy"):
            symbol_missing += 1
    return file_missing, symbol_missing


def _consistency_for_tested(case: dict[str, str]) -> int:
    return 1 if case["verif_refs"] == "empty" else 0


def _consistency_for_implemented(case: dict[str, str]) -> int:
    violations = 0
    if case["impl_refs"] == "empty":
        violations += 1
    if case["verif_refs"] == "empty":
        violations += 1
    return violations


def _consistency_for_deviation(case: dict[str, str]) -> int:
    return 1 if case["rationale"] == "empty" else 0


_CONSISTENCY_RULES: dict[str, _RuleFn] = {
    "tested": _consistency_for_tested,
    "implemented": _consistency_for_implemented,
    "deviation": _consistency_for_deviation,
}


def _predict_consistency(case: dict[str, str]) -> int:
    """Return the consistency-violation count implied by the row.

    Dispatches on status; statuses with no rule (spec, n_a) yield 0.
    """
    rule = _CONSISTENCY_RULES.get(case["status"])
    return rule(case) if rule else 0


def _predict_outcome(case: dict[str, str]) -> dict[str, int]:
    """Compute expected summary counters from a PICT row.

    Deliberately simpler and structured differently from the audit's
    own logic so this isn't the audit grading itself. Operates only on
    the row's parameter values and the documented consistency rules.
    """
    file_missing, symbol_missing = _predict_resolution(case)
    consistency = _predict_consistency(case)
    rows_with_gap = (
        1 if (file_missing or symbol_missing or consistency) else 0
    )
    return {
        "refs_file_missing": file_missing,
        "refs_symbol_missing": symbol_missing,
        "consistency_violations": consistency,
        "rows_with_gap": rows_with_gap,
    }


# --- The single parametrized test --------------------------------------


def _case_id(case: dict[str, str]) -> str:
    """Compact one-line ID for pytest output."""
    return ",".join(f"{k}={v}" for k, v in case.items())


@pytest.mark.parametrize(
    "case", _CASES, ids=[_case_id(c) for c in _CASES],
)
def test_audit_combinatorial(
    case: dict[str, str], tmp_path: Path,
) -> None:
    """Materialize one PICT row, run audit, assert predicted outcome."""
    impl_refs: list[str] = []
    verif_refs: list[str] = []
    if case["impl_refs"] != "empty":
        impl_refs.append(_materialize_ref(
            "impl", case["impl_refs"], case["ref_form"],
            case["file_ext"], tmp_path,
        ))
    if case["verif_refs"] != "empty":
        verif_refs.append(_materialize_ref(
            "verif", case["verif_refs"], case["ref_form"],
            case["file_ext"], tmp_path,
        ))
    rationale = (
        "row's reason" if case["rationale"] == "populated" else ""
    )

    ontology = Ontology(domain_constraints=[
        DomainConstraint(
            name="combinatorial",
            description="d",
            status=cast(RequirementStatus, case["status"]),
            rationale=rationale,
            implementation_refs=impl_refs,
            verification_refs=verif_refs,
        ),
    ])
    dag_path = tmp_path / "dag.json"
    dag = OntologyDAG(project_name="iomoments")
    save_snapshot(dag, ontology, label="pict")
    dag_path.write_text(dag.to_json(), encoding="utf-8")

    report = run_audit(dag_path, tmp_path)
    s = report.summary
    expected = _predict_outcome(case)
    cid = _case_id(case)
    exp_file = expected["refs_file_missing"]
    exp_sym = expected["refs_symbol_missing"]
    exp_cons = expected["consistency_violations"]
    exp_gap = expected["rows_with_gap"]

    assert s.refs_file_missing == exp_file, (
        f"file_missing mismatch on {cid}: "
        f"audit={s.refs_file_missing} expected={exp_file}"
    )
    assert s.refs_symbol_missing == exp_sym, (
        f"symbol_missing mismatch on {cid}: "
        f"audit={s.refs_symbol_missing} expected={exp_sym}"
    )
    assert s.consistency_violations == exp_cons, (
        f"consistency_violations mismatch on {cid}: "
        f"audit={s.consistency_violations} expected={exp_cons}"
    )
    assert s.rows_with_gap == exp_gap, (
        f"rows_with_gap mismatch on {cid}: "
        f"audit={s.rows_with_gap} expected={exp_gap}"
    )


def test_pict_yielded_nonempty_case_set() -> None:
    """If PICT was found, it must produce >0 rows. Zero indicates a
    malformed model file or a constraint that excludes everything."""
    assert _CASES, (
        "PICT yielded zero rows — audit_negatives.pict is malformed "
        "or constraints exclude every combination"
    )
