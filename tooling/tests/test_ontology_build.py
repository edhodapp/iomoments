"""Phase 5 tests (D009): YAML-sourced builder.

Covers:
- Round trip of a representative YAML document into an Ontology
  via the pydantic schema.
- Builder idempotency (second run on unchanged content is a no-op).
- Pydantic validation on bad YAML (bogus verdict kind, bad literal,
  duplicate natural key) surfaces as a ValidationError with the
  field path in the message.
- Non-dict YAML root is rejected with a clear error.
- Content hash intentionally sensitive to description whitespace
  (so a prose-only edit DOES create a DAG snapshot — the diff is
  the audit signal).
- main() / _parse_args() CLI plumbing.
- Missing-source file surfaces a clean error.
- The shipped tooling/iomoments-ontology.yaml loads successfully
  and the shipped tooling/iomoments-ontology.json is in sync with
  the YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from iomoments_ontology.build import (
    _build_ontology,
    _load_yaml_source,
    _parse_args,
    build,
    main,
)
from iomoments_ontology import (
    Ontology,
    load_dag,
    ontology_content_hash,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_YAML = _REPO_ROOT / "tooling" / "iomoments-ontology.yaml"
_SHIPPED_JSON = _REPO_ROOT / "tooling" / "iomoments-ontology.json"


# --- _load_yaml_source ---------------------------------------------------


def test_load_yaml_source_accepts_mapping(tmp_path: Path) -> None:
    """A YAML file with a mapping root loads as a dict."""
    src = tmp_path / "ont.yaml"
    src.write_text("entities: []\n", encoding="utf-8")
    data = _load_yaml_source(src)
    assert data == {"entities": []}


def test_load_yaml_source_rejects_non_mapping(tmp_path: Path) -> None:
    """A YAML file whose root is a list / scalar is rejected up front."""
    src = tmp_path / "ont.yaml"
    src.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be a mapping"):
        _load_yaml_source(src)


def test_load_yaml_source_accepts_empty_file(tmp_path: Path) -> None:
    """An empty YAML file loads as an empty dict (safe_load yields None)."""
    src = tmp_path / "ont.yaml"
    src.write_text("", encoding="utf-8")
    assert _load_yaml_source(src) == {}


# --- _build_ontology -----------------------------------------------------


def test_build_ontology_validates_schema(tmp_path: Path) -> None:
    """A YAML source that violates a literal fails with ValidationError."""
    src = tmp_path / "bad.yaml"
    src.write_text(
        yaml.safe_dump(
            {
                "verdict_nodes": [
                    {"kind": "magenta", "description": "nope"},
                ],
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        _build_ontology(src)


def test_build_ontology_rejects_duplicate_moment_rep(tmp_path: Path) -> None:
    """The Ontology-level dedup validator surfaces through YAML load."""
    src = tmp_path / "dup.yaml"
    src.write_text(
        yaml.safe_dump(
            {
                "moment_representations": [
                    {"space": "raw", "order": 1},
                    {"space": "raw", "order": 1},
                ],
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValidationError, match="duplicate MomentRepresentation",
    ):
        _build_ontology(src)


def test_build_ontology_round_trips_sample_yaml(tmp_path: Path) -> None:
    """YAML → Ontology → model_dump preserves content."""
    ont_src = {
        "entities": [
            {"id": "w", "name": "W", "description": "workload"},
        ],
        "domain_constraints": [
            {"name": "c1", "description": "d1", "status": "spec"},
        ],
    }
    src = tmp_path / "sample.yaml"
    src.write_text(yaml.safe_dump(ont_src), encoding="utf-8")
    ont = _build_ontology(src)
    assert isinstance(ont, Ontology)
    assert len(ont.entities) == 1 and ont.entities[0].id == "w"
    assert ont.domain_constraints[0].status == "spec"


# --- build (end-to-end) --------------------------------------------------


def test_build_appends_then_elides(tmp_path: Path) -> None:
    """First build appends; second build with same source is no-op."""
    src = tmp_path / "ont.yaml"
    src.write_text(
        yaml.safe_dump(
            {
                "entities": [
                    {"id": "m", "name": "Moment", "description": "M"},
                ],
            },
        ),
        encoding="utf-8",
    )
    dag_path = tmp_path / "dag.json"

    first_id, first_created = build(src, dag_path)
    second_id, second_created = build(src, dag_path)

    assert first_created is True
    assert second_created is False
    assert first_id == second_id

    dag = load_dag(str(dag_path), project_name="iomoments")
    assert len(dag.nodes) == 1


def test_build_appends_on_content_change(tmp_path: Path) -> None:
    """Edit the YAML, rebuild — a new snapshot is appended."""
    src = tmp_path / "ont.yaml"
    dag_path = tmp_path / "dag.json"

    src.write_text(yaml.safe_dump({}), encoding="utf-8")
    build(src, dag_path)

    src.write_text(
        yaml.safe_dump(
            {"domain_constraints": [{"name": "c", "description": "d"}]},
        ),
        encoding="utf-8",
    )
    _, created = build(src, dag_path)
    assert created is True

    dag = load_dag(str(dag_path), project_name="iomoments")
    assert len(dag.nodes) == 2
    assert len(dag.edges) == 1


# --- Shipped artifact sanity --------------------------------------------


def test_shipped_yaml_loads_cleanly() -> None:
    """The shipped tooling/iomoments-ontology.yaml validates."""
    ont = _build_ontology(_SHIPPED_YAML)
    assert isinstance(ont, Ontology)
    # Sanity-check the draft content matches our Phase 5 scope.
    assert len(ont.verdict_nodes) == 4
    assert len(ont.diagnostic_signals) == 5
    assert len(ont.moment_representations) == 6
    assert len(ont.entities) >= 4
    assert len(ont.open_questions) >= 1


def test_shipped_json_matches_shipped_yaml() -> None:
    """Shipped DAG's current node hash matches the shipped YAML's hash.

    If this fails, the YAML source was edited but the DAG wasn't
    rebuilt; run `build-iomoments-ontology` and commit both.
    """
    if not _SHIPPED_JSON.exists():
        pytest.skip("shipped DAG not yet built")
    ont_from_yaml = _build_ontology(_SHIPPED_YAML)
    dag = load_dag(str(_SHIPPED_JSON), project_name="iomoments")
    current = dag.get_current_node()
    assert current is not None
    assert ontology_content_hash(ont_from_yaml) == ontology_content_hash(
        current.ontology,
    )


# --- Content hash whitespace sensitivity --------------------------------


def test_content_hash_is_whitespace_sensitive(tmp_path: Path) -> None:
    """Description whitespace changes DO change the content hash.

    This pins an intentional design choice: a prose-only edit to a
    description string is NOT elided as a no-op. The rationale: the
    ontology is a formal-requirements artifact where description
    text IS audit evidence; a whitespace-only reformat is a
    meaningful edit worth capturing in the DAG history.

    If we ever want whitespace-insensitive equivalence, normalize
    before hashing rather than weakening the canonical form.
    """
    def _build(desc: str) -> str:
        src = tmp_path / f"{hash(desc)}.yaml"
        src.write_text(
            yaml.safe_dump(
                {
                    "entities": [
                        {"id": "x", "name": "X", "description": desc},
                    ],
                },
            ),
            encoding="utf-8",
        )
        return ontology_content_hash(_build_ontology(src))

    assert _build("hello world") != _build("hello  world")
    assert _build("hello world") != _build("hello world ")


# --- main() / _parse_args() CLI plumbing --------------------------------


def test_main_with_explicit_argv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() accepts an explicit argv list and reports status."""
    src = tmp_path / "ont.yaml"
    src.write_text("entities: []\n", encoding="utf-8")
    dag_path = tmp_path / "dag.json"
    rc = main(
        ["--source", str(src), "--dag", str(dag_path)],
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "appended" in captured.out


def test_main_without_argv_reads_sys_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() with no argument falls back to sys.argv[1:]."""
    src = tmp_path / "ont.yaml"
    src.write_text("entities: []\n", encoding="utf-8")
    dag_path = tmp_path / "dag.json"
    monkeypatch.setattr(
        "sys.argv",
        ["build-iomoments-ontology",
         "--source", str(src),
         "--dag", str(dag_path)],
    )
    rc = main()
    assert rc == 0
    assert "appended" in capsys.readouterr().out


def test_parse_args_returns_path_types() -> None:
    """--source / --dag come back as Path objects, not strings."""
    ns = _parse_args(["--source", "/tmp/a.yaml", "--dag", "/tmp/b.json"])
    assert isinstance(ns.source, Path)
    assert isinstance(ns.dag, Path)
    assert ns.source == Path("/tmp/a.yaml")


def test_build_raises_on_missing_source(tmp_path: Path) -> None:
    """Missing YAML source surfaces FileNotFoundError cleanly."""
    missing = tmp_path / "nope.yaml"
    dag_path = tmp_path / "dag.json"
    with pytest.raises(FileNotFoundError):
        build(missing, dag_path)
