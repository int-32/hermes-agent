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
    _run_probe("kanban_board_fallback_probe.js")


def test_fallback_to_default_drops_the_refused_boards_filters():
    """Falling back must reset cross-board state exactly like switchBoard: board X opened with a
    project/tenant/search filter, then X vanished (404). The default board used to load with
    X's ``project_id``/``tenant`` and could render empty. Mounts the real KanbanPage on a minimal
    hooks runtime and asserts on the requests it sends and the pin it stores.
    """
    _run_probe("kanban_board_fallback_reset_probe.js")


def _run_probe(name: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    bundle = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    result = subprocess.run(
        [node, str(Path(__file__).parent / "fixtures" / name), str(bundle)],
        capture_output=True, text=True, timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout
