"""D015 sub-commit 7g: producer that converts aws_tracer.sh output into
TestResult records.

The AWS probe (scripts/aws_tracer.sh) writes per-distro outputs to
build/aws-tracer/<distro>/{meta.txt, k4.verdict, k3.verdict, ...}.
This producer reads those files, builds one TestResult per distro
× variant pair where the verdict was 0 (pass), and appends them
to the test-results DAG via the existing transaction machinery.

Usage::

    python tooling/aws_tracer_producer.py \\
        [--results-dir build/aws-tracer] \\
        [--dag tooling/iomoments-test-results.json] \\
        [--repo-root .]

Invoked automatically by scripts/aws_tracer.sh at the end of a
probe run (before teardown). Can also be run manually against a
prior probe's results without re-running the probe — though
captured_git_sha will reflect HEAD at producer-invocation time,
not probe-invocation time, so re-running the probe gives a more
honest record.

Verification-ref shape: the probe is a single executable script,
not a library of testable symbols. Refs use the file-only form
``scripts/aws_tracer.sh``. Variant (k4 / k3) and distro / kernel
information lives in EnvironmentSpec — env.flags["variant"]
distinguishes k4 from k3 against the same distro, env.kernel and
env.distro carry the as-observed values from meta.txt.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from audit_ontology import git_helpers
from iomoments_ontology import (
    EnvironmentSpec,
    TestResult,
    TestResultsSnapshot,
    prune_and_add_result,
    prune_test_results_dag_nodes,
    snapshot_test_results_if_changed,
    test_results_dag_transaction,
)


_VERIFICATION_REF = "scripts/aws_tracer.sh"
_FIX_RECIPE = "bash scripts/aws_tracer.sh"


def _parse_meta(meta_path: Path) -> dict[str, str]:
    """Read distro/<distro>/meta.txt → dict of fields we care about.

    The probe writes uname -r on the first line, then lines from
    /etc/os-release (NAME=, VERSION_ID=, PRETTY_NAME=), then
    AMI= and InstanceId= lines we appended. We only need the kernel
    (line 1) and the AMI / InstanceId pair for diagnostics; the
    distro slug comes from the directory name.
    """
    out: dict[str, str] = {}
    if not meta_path.is_file():
        return out
    text = meta_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines:
        out["kernel"] = lines[0].strip()
    for line in lines[1:]:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def _parse_verdict(verdict_path: Path) -> int | None:
    """Return the integer exit code from a *.verdict file, or None."""
    if not verdict_path.is_file():
        return None
    text = verdict_path.read_text(encoding="utf-8").strip()
    # Format: "VERDICT=0" or "VERDICT=255"
    if not text.startswith("VERDICT="):
        return None
    try:
        return int(text.split("=", 1)[1])
    except (ValueError, IndexError):
        return None


def _build_results(
    results_dir: Path, head_sha: str, captured_at: datetime,
) -> list[TestResult]:
    """Walk results_dir for distro subdirs, emit one TestResult per
    (distro, variant) pair where verdict was 0 (pass).

    Per D015 §6, only outcome="pass" is stored — non-zero verdicts
    (verifier rejection, instance failure) are not represented.
    """
    out: list[TestResult] = []
    for distro_dir in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        meta = _parse_meta(distro_dir / "meta.txt")
        kernel = meta.get("kernel", "")
        distro_slug = distro_dir.name
        for variant in ("k4", "k3"):
            verdict = _parse_verdict(distro_dir / f"{variant}.verdict")
            if verdict != 0:
                continue
            env = EnvironmentSpec(
                kind="aws-ec2",
                kernel=kernel,
                distro=distro_slug,
                flags={"variant": variant},
                fix_recipe=_FIX_RECIPE,
            )
            out.append(TestResult(
                verification_ref=_VERIFICATION_REF,
                environment=env,
                outcome="pass",
                captured_git_sha=head_sha,
                captured_at=captured_at,
            ))
    return out


def emit(
    results_dir: Path, dag_path: Path, repo_root: Path,
) -> int:
    """Emit AWS-probe TestResults to the DAG. Returns count emitted.

    Returns 0 on no-op (no results dir, no passing variants, no git
    head). Returns the count of TestResult records written
    otherwise. Caller doesn't need to distinguish "0 because nothing
    to emit" from "0 because dedupe folded everything" — the DAG
    state is the source of truth either way.
    """
    if not results_dir.is_dir():
        return 0
    head = git_helpers.head_sha(repo_root)
    if head is None:
        return 0

    captured_at = datetime.now(timezone.utc)
    new_results = _build_results(results_dir, head, captured_at)
    if not new_results:
        return 0

    with test_results_dag_transaction(
        str(dag_path), project_name="iomoments",
    ) as dag:
        current = dag.get_current_node()
        snapshot = (
            current.snapshot
            if current is not None
            else TestResultsSnapshot()
        )
        for r in new_results:
            snapshot = prune_and_add_result(snapshot, r)
        snapshot_test_results_if_changed(
            dag, snapshot, label="aws-probe",
        )
        prune_test_results_dag_nodes(dag)
    return len(new_results)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns standard Unix exit codes.

    Exit 0 always — producer never gates pytest / probe pipelines.
    Diagnostic info goes to stdout; problems print to stderr but
    don't fail the run.
    """
    repo_root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Emit AWS-probe TestResults to the test-results DAG.",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=repo_root_default / "build" / "aws-tracer",
    )
    parser.add_argument(
        "--dag",
        type=Path,
        default=(
            repo_root_default
            / "tooling"
            / "iomoments-test-results.json"
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=repo_root_default,
    )
    args = parser.parse_args(argv)
    count = emit(args.results_dir, args.dag, args.repo_root)
    print(
        f"aws_tracer_producer: emitted {count} TestResult(s) to "
        f"{args.dag}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
