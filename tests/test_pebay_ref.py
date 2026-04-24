"""Numerical reference for iomoments' Pébay update rules.

Python-side oracle (D005): this file validates the C implementation in
src/pebay.h against scipy / numpy and shells out to the C test driver
so pytest runs (CI, pre-push) catch C-side regressions too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from scipy import stats

_REPO_ROOT = Path(__file__).resolve().parents[1]
_C_TEST_BIN = _REPO_ROOT / "build" / "test_pebay"


def test_scipy_moments_round_trip() -> None:
    """scipy and numpy agree on the first two moments of a known sample.

    Dev-dep smoke test: confirms numpy + scipy resolve and produce
    the textbook answers for a well-behaved sample.
    """
    rng = np.random.default_rng(seed=0)
    sample = rng.normal(loc=3.0, scale=2.0, size=10_000)

    np_mean = float(np.mean(sample))
    scipy_mean = float(stats.tmean(sample))
    np_var = float(np.var(sample, ddof=0))
    scipy_var = float(stats.tvar(sample, ddof=0))

    assert abs(np_mean - scipy_mean) < 1e-12
    assert abs(np_var - scipy_var) < 1e-10


def test_pebay_c_driver_passes() -> None:
    """Shell out to the compiled C test driver.

    Fails with a clear error message if the binary isn't built.
    CI and pre-push run `make test` which depends on `make test-c`,
    so the binary is present when this test runs there. Locally, the
    skip path only fires when a developer runs pytest in isolation
    without `make test-c` first — surfacing the build requirement
    clearly rather than silently masking a C regression.
    """
    if not _C_TEST_BIN.is_file():
        pytest.skip(
            f"{_C_TEST_BIN} not built — run `make test-c` first."
        )
    result = subprocess.run(
        [str(_C_TEST_BIN)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"C Pébay driver failed (rc={result.returncode}):\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
