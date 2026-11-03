"""Real dispatcher + native API proof before external registry writes are enabled."""

import json
import sys
from pathlib import Path

import pytest

from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from tests.waiting import wait_until


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HERMES_HOME", "HERMES_KANBAN_HOME"):
        monkeypatch.setenv(name, str(home))
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Keep main's worker-client argv and the production _default_spawn/Popen path;
    # substitute only the executable with a harmless local recorder.
    marker = tmp_path / "spawn.json"
    worker = tmp_path / "fixture-worker"
    worker.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(json.dumps(sys.argv))\n"
    )
    worker.chmod(0o700)
    worker_argv = dispatch._worker_argv
    def harmless_argv(*args):
        argv = worker_argv(*args)
        assert argv[:4] == [sys.executable, '-P', '-m', 'hermes_cli.kanban_worker_client']
        return [str(worker), *argv[1:]]
    monkeypatch.setattr(dispatch, '_worker_argv', harmless_argv)
    kb.create_board("tracking")
    return home, marker


def test_native_sticky_registration_survives_live_ticks_and_reconstruction(board):
    home, marker = board
    settings = _resolve_dispatcher_settings({"default_assignee": "default"}, kb)
    dispatcher = _KanbanDispatcher(kb, settings)
    # Start the service before registration; discover a newly populated board.
    assert all(not result.spawned for _, result in dispatcher.tick_once())
    with kbc.connect_closing(board="tracking") as conn:
        tid = kb.create_task(conn, title="external work", assignee="default",
                             initial_status="blocked", board="tracking")
        assert kb.claim_task(conn, tid) is None
        assert kb.claim_review_task(conn, tid) is None
    for service in (dispatcher, _KanbanDispatcher(kb, settings)):
        for _ in range(3):
            results = service.tick_once()
            assert all(not r.spawned and r.promoted == 0 for _, r in results)
            assert service.auto_decompose_tick(3) == 0
    with kbc.connect_closing(board="tracking") as conn:
        task = kb.get_task(conn, tid)
        assert (task.status, task.current_run_id, task.claim_lock) == ("blocked", None, None)
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
    assert not marker.exists()
    assert not (home / "config.yaml").exists()


def test_positive_control_real_default_spawn_on_ready_card(board):
    _, marker = board
    with kbc.connect_closing(board="tracking") as conn:
        tid = kb.create_task(conn, title="harmless control", assignee="default", board="tracking")
    service = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    result = service.tick_once_for_board("tracking")
    assert [row[0] for row in result.spawned] == [tid]
    assert wait_until(marker.exists, timeout=10)
    assert json.loads(marker.read_text())[1:] == ['-P', '-m', 'hermes_cli.kanban_worker_client']
