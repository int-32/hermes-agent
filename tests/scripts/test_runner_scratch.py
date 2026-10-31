"""Checkout-local runner scratch keeps native state isolated without widening the guard."""
import os
from pathlib import Path
import tempfile

import pytest

from scripts import run_tests_parallel as runner


def test_scratch_root_refuses_checkout_and_external_directories(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for root in (checkout, tmp_path / "external"):
        with pytest.raises(ValueError, match="inside this checkout"):
            runner._checkout_scratch_root(str(root), checkout)
    assert not (tmp_path / "external").exists()
    chosen = checkout / "scratch"
    assert runner._checkout_scratch_root(str(chosen), checkout) == str(chosen.resolve())


def test_explicit_scratch_is_used_by_tempfiles_and_session_sandbox():
    from tests.conftest import HERMES_HOME_AT_CONFTEST_IMPORT

    supplied = os.environ.get("HERMES_TEST_RUNNER_SCRATCH")
    if supplied:
        root = Path(supplied)
        assert Path(tempfile.gettempdir()).is_relative_to(root)
        assert Path(HERMES_HOME_AT_CONFTEST_IMPORT).is_relative_to(root)
    # This assertion runs with the ordinary guard installed, even when the sandbox is
    # under the source prefix that the guard permits for imports and tracebacks.
    from tests.home_io_guard import HomeIOGuard
    protected = Path(__file__).resolve().parent / "guard-probe"
    with pytest.raises(AssertionError, match="REAL hermes home"):
        HomeIOGuard(lambda: [protected]).check(protected / "state.db")
