"""Tracking boards retain native columns without native claims, attempts or workers."""
import json
import sys
from pathlib import Path

import pytest

from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.waiting import wait_until


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for key in ('HERMES_HOME', 'HERMES_KANBAN_HOME'):
        monkeypatch.setenv(key, str(home))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD'):
        monkeypatch.delenv(key, raising=False)
    marker = tmp_path / 'spawn.json'
    worker = tmp_path / 'fixture-worker'
    worker.write_text(f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(json.dumps(sys.argv))\n')
    worker.chmod(0o700)
    monkeypatch.setenv('HERMES_BIN', str(worker))
    kb.create_board('tracking')
    return home, marker


def test_tracking_mode_survives_connections_and_real_dispatcher_reconstruction(board):
    _, marker = board
    service = _KanbanDispatcher(kb, _resolve_dispatcher_settings({'default_assignee': 'default'}, kb))
    service.tick_once()
    with kbc.connect_closing(board='tracking') as conn:
        kb.set_board_tracking_only(conn, True)
        tid = kb.create_task(conn, title='External executor', assignee='default', board='tracking')
        for status in ('running', 'review', 'done', 'ready', 'triage'):
            assert kb.observe_tracking_task(conn, tid, status, actor='fixture', evidence={'goal_status': 'accepted', 'result': 'Accepted fixture.'})
            assert kb.get_task(conn, tid).status == status
            with kbc.connect_closing(board='tracking') as reopened:
                assert kb.get_task(reopened, tid).current_run_id is None
                assert kb.list_runs(reopened, tid) == []
            assert kb.claim_task(conn, tid) is None
            assert kb.claim_review_task(conn, tid) is None
            for dispatcher in (service, _KanbanDispatcher(kb, service.settings)):
                result = dispatcher.tick_once_for_board('tracking')
                assert not result.spawned and result.promoted == result.reclaimed == 0
                assert dispatcher.auto_decompose_tick(3) == 0
            assert kb.get_task(conn, tid).status == status
        assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0
        assert not any((kb.get_task(conn, tid).claim_lock, kb.get_task(conn, tid).current_run_id, kb.get_task(conn, tid).worker_pid))
    with kbc.connect_closing(board='tracking') as conn:
        assert kb.board_tracking_only(conn) is True
        with pytest.raises(ValueError, match='empty'):
            kb.set_board_tracking_only(conn, False)
    assert not marker.exists()


def test_native_board_positive_control_uses_real_spawn(board):
    _, marker = board
    with kbc.connect_closing(board='tracking') as conn:
        tid = kb.create_task(conn, title='Local positive control', assignee='default', board='tracking')
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    result = dispatcher.tick_once_for_board('tracking')
    assert [item[0] for item in result.spawned] == [tid]
    assert wait_until(marker.exists, timeout=10)
    assert 'chat' in json.loads(marker.read_text())


def test_tracking_observation_preserves_native_acceptance_and_child_fences(board, monkeypatch):
    kb.create_board('observations', tracking_only=True)
    with kbc.connect_closing(board='observations') as conn:
        tid = kb.create_task(conn, title='Native PR gate', completion_contract='fixture/repo', board='observations')
        with pytest.raises(ValueError, match='PR acceptance'):
            kb.observe_tracking_task(conn, tid, 'done', actor='fixture', evidence={'goal_status': 'accepted', 'result': 'Only external evidence'})
        assert not kb.list_runs(conn, tid)
        with pytest.raises(ValueError, match='accepted goal'):
            kb.observe_tracking_task(conn, tid, 'done', actor='fixture', evidence={'result': 'Not accepted'})
    # The existing delegated-child boundary applies even to this composable API.
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', '1')
    with kbc.connect_closing(board='observations') as conn, pytest.raises(PermissionError, match='delegate_task child'):
        kb.observe_tracking_task(conn, tid, 'running', actor='fixture', evidence={})


def test_invalid_policy_cannot_reinitialize_as_dispatchable(board):
    kb.create_board('closed', tracking_only=True)
    with kbc.connect_closing(board='closed') as conn:
        conn.execute('DELETE FROM kanban_board_policy')
        conn.commit()
    with pytest.raises(ValueError, match='policy is missing or invalid'):
        kb.init_db(board='closed')
