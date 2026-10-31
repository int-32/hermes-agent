"""Tracking boards retain native columns without native claims, attempts or workers."""
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

import psutil
import pytest

from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home
from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket


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


def test_native_board_positive_control_uses_real_spawn(board, tmp_path):
    home, marker = board
    root = Path(__file__).resolve().parents[2]
    user, workspace = tmp_path / 'user', home / 'workspace'
    user.mkdir()
    workspace.mkdir()
    entered, release = threading.Event(), threading.Event()
    responses = iter([ToolCall('kanban_complete', {'summary': 'Isolated native positive control'}),
                      Text('NATIVE_OWNER_COMPLETE')])

    def answer(_request):
        entered.set()
        assert release.wait(90), 'fixture model barrier was not released'
        return next(responses)

    with FakeLLMServer(answer) as model:
        write_hermes_home(home, model.base_url, extra_config='''
gateway:
  multiplex_profiles: false
kanban:
  dispatch_in_gateway: false
platform_toolsets:
  cli: [file]
''')
        env = child_env()
        env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
            HERMES_KANBAN_HOME=str(home), HERMES_RUNTIME_DIR=str(tmp_path / 'empty-pm'),
            PYTHONPATH=str(root), PYTHONUNBUFFERED='1', NO_PROXY='*',
            HTTP_PROXY='', HTTPS_PROXY='', ALL_PROXY='')
        if os.name != 'nt':
            runtime = Path('/tmp') / ('hwe-' + hashlib.sha256(str(root).encode()).hexdigest()[:12])  # no-tmp: ok — official AF_UNIX socket length limit
            runtime.mkdir(mode=0o700, exist_ok=True)
            env['XDG_RUNTIME_DIR'] = str(runtime)
        with kbc.connect_closing(board='tracking') as conn:
            tid = kb.create_task(conn, title='Local positive control', assignee='default', board='tracking',
                workspace_kind='dir', workspace_path=str(workspace))
        client = None
        with daemon(root, home, env, barrier=False) as (owner, descriptor):
            try:
                dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
                result = dispatcher.tick_once_for_board('tracking')
                assert [item[0] for item in result.spawned] == [tid]
                with kbc.connect_closing(board='tracking') as conn:
                    client = psutil.Process(kb.get_task(conn, tid).worker_pid)
                assert entered.wait(60), 'native worker did not reach the isolated owner/model'
                argv = client.cmdline()
                assert argv[-2:] == ['-m', 'hermes_cli.kanban_worker_client'], argv
                with kbc.connect_closing(board='tracking') as conn:
                    task = kb.get_task(conn, tid)
                    assert task.worker_pid == client.pid
                    params = dict(board='tracking', task_id=tid, run_id=task.current_run_id,
                                  claim_lock=task.claim_lock)
                    admitted = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                        "AND kind='owner_admitted'", (tid, task.current_run_id)).fetchall()
                    assert len(admitted) == 1
                    admission = json.loads(admitted[0][0])
                    assert admission['pid'] == owner.pid
                    assert Path(admission['db']) == home / 'state.db'
                release.set()
                assert client.wait(timeout=60) == 0

                async def receipt():
                    async with websocket(home, descriptor, proxy=None) as ws:
                        first = await rpc(ws, 'kanban.run', **params)
                        again = await rpc(ws, 'kanban.run', **params)
                        assert 'result' in first and again == first, (first, again)
                        return first['result']

                accepted = asyncio.run(receipt())
                assert accepted['session_id'] == admission['session_id']
                assert accepted['receipt']['status'] == 'terminal'
                assert accepted['receipt']['outcome'] == 'completed'
                with sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True) as db:
                    rows = db.execute('SELECT admission_id,status,outcome FROM session_admissions '
                        'WHERE request_id=?', (admission['request_id'],)).fetchall()
                assert rows == [(accepted['receipt']['admission_id'], 'terminal', 'completed')]
                from gateway.session_kanban import worker_result
                outcome = worker_result(kb.kanban_db_path(board='tracking'), params)
                assert outcome['exit_code'] == 0 and outcome['last_output'] == 'NATIVE_OWNER_COMPLETE'
                assert 'NATIVE_OWNER_COMPLETE' in kb.read_worker_log(tid, board='tracking')
                assert len(model.main_requests()) == 2
                with kbc.connect_closing(board='tracking') as conn:
                    assert kb.get_task(conn, tid).status == 'done'
                    assert len(kb.list_runs(conn, tid)) == 1
                assert not marker.exists(), 'legacy HERMES_BIN must not replace the native client'
                print(json.dumps(dict(client_pid=client.pid, argv=argv, owner_pid=owner.pid,
                    owner_admitted=admission, receipt=accepted, worker_result=outcome)))
            finally:
                release.set()
                if client is not None and client.is_running():
                    client.kill()
                    client.wait(timeout=10)


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
