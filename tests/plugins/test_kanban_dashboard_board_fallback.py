"""Dashboard bundle: a refused pinned board falls back to the default board."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_refused_pinned_board_falls_back_to_default():
    """A board pinned in localStorage that the server (or a reverse proxy exposing only some
    boards) refuses with 403, or no longer has (404), used to render the load-failed card on
    every reload: the card has no switcher and /boards?board=<pin> is refused too, so the
    stale-slug fallback never ran. Runs the real decision function from the bundle (extracted
    verbatim) against the ApiError shapes the host SDK throws.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bundle = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    probe = Path(__file__).parent / "fixtures" / "kanban_board_fallback_probe.js"
    result = subprocess.run(
        [node, str(probe), str(bundle)],
        capture_output=True, text=True, timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout
