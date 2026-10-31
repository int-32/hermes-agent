"""Real plugin status edits retain native columns without executing external work."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
from tests.plugins.test_kanban_board_project_api import _load_plugin_router


def test_tracking_api_project_filter_and_manual_states(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    kb.create_board('tracking', tracking_only=True)
    from hermes_cli import projects_db as pdb
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name='Fixture', primary_path=str(tmp_path))
    with kbc.connect_closing(board='tracking') as conn:
        tid = kb.create_task(conn, title='External card', project_id=pid, workspace_kind='dir', workspace_path=str(tmp_path), board='tracking')
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix='/api/plugins/kanban')
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    with TestClient(app) as client:
        for status in ('running', 'review', 'done', 'ready'):
            response = client.patch(f'/api/plugins/kanban/tasks/{tid}?board=tracking', json={'status': status, 'result': 'User accepted'})
            assert response.status_code == 200, response.text
            result = dispatcher.tick_once_for_board('tracking')
            assert not result.spawned and not result.reclaimed and not result.promoted
            data = client.get(f'/api/plugins/kanban/board?board=tracking&project_id={pid}').json()
            assert data['tracking_only'] is True
            assert [t['id'] for c in data['columns'] for t in c['tasks']] == [tid]
            assert next(c['name'] for c in data['columns'] if c['tasks']) == status
        data = client.get('/api/plugins/kanban/board?board=tracking&project_id=p_other').json()
        assert not any(c['tasks'] for c in data['columns'])
    with kbc.connect_closing(board='tracking') as conn:
        assert not kb.list_runs(conn, tid)
        assert kb.claim_task(conn, tid) is None
