"""Kanban card provenance replaces the independent coordination task list."""
import json
import sqlite3
from tests.gateway.test_agent_handoff import native_owner

import pytest

from gateway.agent_coordination import submit_document
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.gateway.test_agent_coordination import make_star
from hermes_cli.kanban_task_source import accept_task, board_path, read_sources

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.asyncio
async def test_card_source_without_static_task_registration(tmp_path, monkeypatch):
    service, path = make_star(tmp_path, monkeypatch, native_tasks=False)
    # Native source metadata is a card event, never a body/comment assertion.
    source = service.runner.entries['default-task'].origin
    home = service.home
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    kb.create_board('registry', tracking_only=True)
    with kbc.connect_closing(board='registry') as conn:
        tid = kb.create_task(conn, title='existing card', initial_status='blocked', board='registry',
                             project_id=service.fixture_project_id)
        kb._append_event(conn, tid, 'native_source_bound', {
            'task_id': 'task-one', 'profile': 'default', 'session_key': 'default-task',
            'session_id': 'default-task-sid', 'profile_home': str(service.runner.home / 'default'),
            'origin': source.to_dict(), 'document_project': 'project', 'project_id': service.fixture_project_id,
        })
        conn.commit()
    service.policy['coordination']['tasks'] = {}
    result = await submit_document(service, 'default-task', 'default-task-sid',
                                   service.runner.home / 'default', 'single-source-01',
                                   'task-one', str(path), 'main')
    assert result['status'] == 'queued'
    await service.tick()
    assert service.journal.get('single-source-01')['status'] == 'delivered'
    assert len(service.runner.received) == 2
    assert not service.runner.sent
    with kbc.connect_closing(board='registry') as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert kb.list_runs(conn, tid) == []
        assert kb.claim_task(conn, tid) is None


async def request(service, document, request_id='source-test-001', **changes):
    values = dict(source_key='default-task', source_sid='default-task-sid',
                  caller_home=service.runner.home / 'default', request_id=request_id,
                  task_id='task-one', path=str(document), target='main')
    values.update(changes)
    return await submit_document(service, **values)


@pytest.mark.asyncio
async def test_static_task_list_is_ignored_and_document_project_is_separate(tmp_path, monkeypatch):
    service, path = make_star(tmp_path, monkeypatch)
    service.policy['coordination']['tasks'] = {'attacker': {'session_key': 'default-task'}}
    result = await request(service, path)
    assert result['status'] == 'queued'
    assert result['document']['project'] == 'project'
    assert read_sources(service.home)['task-one']['native_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['body-only', 'missing-card', 'duplicate-source', 'unreadable-store',
                                     'project-changed', 'wrong-document-project', 'invalid-sid', 'wrong-home'])
async def test_no_task_or_identity_fallback(tmp_path, monkeypatch, mutation):
    service, path = make_star(tmp_path, monkeypatch)
    specs = read_sources(service.home)
    tid = specs['task-one']['native_id']
    with kbc.connect_closing(board='registry') as conn:
        def body_only():
            conn.execute('DELETE FROM task_events WHERE task_id=? AND kind=?', (tid, 'native_source_bound'))
            kb.edit_task(conn, tid, body=json.dumps(specs['task-one']))
            kb.add_comment(conn, tid, 'owner', json.dumps(specs['task-one']))
        def duplicate_source():
            other = kb.create_task(conn, title='forged', board='registry')
            copy = dict(specs['task-one'], task_id='other-task')
            kb._append_event(conn, other, 'native_source_bound', copy)
        mutate = {'body-only': body_only, 'duplicate-source': duplicate_source,
                  'missing-card': lambda: conn.execute('DELETE FROM task_events WHERE task_id=?', (tid,)),
                  'project-changed': lambda: conn.execute('UPDATE tasks SET project_id=? WHERE id=?', ('other-project', tid))}
        if mutation in mutate:
            mutate[mutation]()
        conn.commit()
    if mutation == 'unreadable-store':
        board_path(service.home).write_bytes(b'invalid SQLite database')
    other = path.parents[2] / 'other-project' / 'task-one' / 'HANDOFF.md'
    if mutation == 'wrong-document-project':
        other.parent.mkdir(parents=True); other.write_text('untrusted')
    changes = {'wrong-document-project': {'path': str(other)}, 'invalid-sid': {'source_sid': 'claimed-sid'},
               'wrong-home': {'caller_home': service.runner.home / 'aiyu-ops'}}.get(mutation, {})
    with pytest.raises((PermissionError, ValueError, sqlite3.Error)):
        await request(service, path, **changes)
    assert service.journal.get('source-test-001') is None
    assert not service.runner.sent and not service.runner.received


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['done', 'archived', 'paused'])
async def test_closed_or_paused_refuses_new_but_returns_existing_work(tmp_path, monkeypatch, status):
    service, path = make_star(tmp_path, monkeypatch)
    await request(service, path)
    record = service.journal.get('source-test-001')
    tid = read_sources(service.home)['task-one']['native_id']
    with kbc.connect_closing(board='registry') as conn:
        if status == 'paused':
            kb.block_task(conn, tid, reason='User pause')
        else:
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid)); conn.commit()
    with pytest.raises(PermissionError, match='closed or paused'):
        await request(service, path, request_id='source-test-002')
    with pytest.raises(PermissionError, match='closed or paused'):
        await service.pinned_routes(record)
    # Returning work is not a fresh dispatch and retains its original edge.
    record = service.journal.update('source-test-001', status='generated', response='old in-flight result')
    origin, target, _ = await service.pinned_routes(record)
    assert origin.chat_id == 'default-task' and target.chat_id == 'default-main'


@pytest.mark.asyncio
async def test_source_compression_continues_but_reset_does_not(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    service, path = make_star(tmp_path, monkeypatch)
    home = service.runner.home / 'default'; home.mkdir(exist_ok=True)
    state = SessionDB(home / 'state.db')
    state.create_session('default-task-sid', source='discord')
    state.end_session('default-task-sid', 'compression')
    state.create_session('compressed-task', source='discord', parent_session_id='default-task-sid')
    state.close()
    service.runner.entries['default-task'].session_id = 'compressed-task'
    assert (await request(service, path, source_sid='compressed-task'))['status'] == 'queued'
    service.runner.entries['default-task'].session_id = 'reset-task'
    with pytest.raises(PermissionError, match='reset or forked'):
        await request(service, path, request_id='source-reset-002', source_sid='reset-task')


@pytest.mark.asyncio
async def test_receipt_migration_uses_pins_and_fifo_never_old_task_admission(tmp_path, monkeypatch):
    service, path = make_star(tmp_path, monkeypatch)
    await request(service, path)
    record = service.journal.get('source-test-001')
    record.pop('task_binding')
    # Simulate a pre-upgrade persisted record through journal API, not live edits.
    service.journal.update('source-test-001', task_binding=None, status='generated',
                           response='pre-upgrade generated result', outcome='completed')
    with kbc.connect_closing(board='registry') as conn:
        conn.execute("DELETE FROM task_events WHERE kind='native_source_bound'"); conn.commit()
    service.runner.busy = True
    await service.tick()
    assert service.journal.get('source-test-001')['status'] == 'generated'
    with pytest.raises(PermissionError):
        await request(service, path, request_id='fresh-refused-002')
    service.runner.busy = False
    await service.tick()
    assert service.journal.get('source-test-001')['status'] == 'delivered'
    assert not service.runner.sent


@pytest.mark.asyncio
async def test_current_project_archive_refuses_new_work_but_retains_return_edge(tmp_path, monkeypatch):
    from hermes_cli import projects_db
    service, path = make_star(tmp_path, monkeypatch)
    await request(service, path)
    with projects_db.connect_closing() as conn:
        projects_db.archive_project(conn, service.fixture_project_id)
    with pytest.raises(PermissionError, match='project missing or archived'):
        await request(service, path, request_id='archive-refused-002')
    record = service.journal.get('source-test-001')
    with pytest.raises(PermissionError, match='project missing or archived'):
        await service.pinned_routes(record)
    record = service.journal.update('source-test-001', status='generated', response='pinned in-flight return')
    source, target, _ = await service.pinned_routes(record)
    assert source.chat_id == 'default-task' and target.chat_id == 'default-main'
    assert not service.runner.sent


def test_acceptance_reuses_card_and_refuses_rebinding(tmp_path, monkeypatch):
    service, _ = make_star(tmp_path, monkeypatch)
    spec = read_sources(service.home)['task-one']
    original = spec['native_id']
    values = dict(policy=service.policy['coordination'], source=service.runner.entries['default-task'].origin,
                  session_key='default-task', session_id='default-task-sid', profile_home=service.runner.home / 'default',
                  task_id='task-one', document_project='project', project_id=spec['project_id'], native_id=original)
    with kbc.connect_closing(board='registry') as conn:
        assert accept_task(conn, **values) == original
        with pytest.raises(PermissionError, match='immutable'):
            accept_task(conn, **dict(values, document_project='another-project'))
        with pytest.raises(PermissionError, match='already belongs'):
            accept_task(conn, **dict(values, task_id='duplicate-task'))
        assert len(kb.list_tasks(conn)) == 2
        assert len([e for e in kb.list_events(conn, original) if e.kind == 'native_source_bound']) == 1
        assert kb.list_runs(conn, original) == []


@pytest.mark.asyncio
async def test_archived_unrelated_project_does_not_block_current_task(tmp_path, monkeypatch):
    from hermes_cli import projects_db
    from gateway.session import SessionSource
    from gateway.config import Platform
    service, path = make_star(tmp_path, monkeypatch)
    with projects_db.connect_closing() as conn:
        project_id = projects_db.create_project(conn, name='Old project', slug='old-project', primary_path=str(tmp_path / 'old-repo'))
    source = SessionSource(Platform.DISCORD, 'old-task', chat_type='thread', user_id='owner',
                           thread_id='old-task', parent_chat_id='projects', scope_id='guild')
    with kbc.connect_closing(board='registry') as conn:
        accept_task(conn, policy=service.policy['coordination'], source=source, session_key='old-task',
                    session_id='old-sid', profile_home=service.runner.home / 'default', task_id='old-task',
                    document_project='old-project', project_id=project_id, title='Historical card')
    with projects_db.connect_closing() as conn:
        projects_db.archive_project(conn, project_id)
    assert (await request(service, path))['status'] == 'queued'
    await service.tick()
    assert service.journal.get('source-test-001')['status'] == 'delivered'


def test_legacy_registration_namespace_does_not_impersonate_runtime_profile(tmp_path, monkeypatch):
    from scripts.kanban_registry_records import task_key
    service, _ = make_star(tmp_path, monkeypatch, native_tasks=False)
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(service.home))
    kb.create_board('registry', tracking_only=True)
    with kbc.connect_closing(board='registry') as conn:
        tid = kb.create_task(conn, title='Controller registered card', board='registry', tenant='default',
                             project_id=service.fixture_project_id,
                             idempotency_key=task_key(dict(profile='default', task_id='migrated-ops-task')))
        result = accept_task(conn, policy=service.policy['coordination'], source=service.runner.entries['ops-task'].origin,
            session_key='ops-task', session_id='ops-task-sid', profile_home=service.runner.home / 'aiyu-ops',
            task_id='migrated-ops-task', document_project='project', project_id=service.fixture_project_id, title='Task')
        assert result == tid
        assert len(kb.list_tasks(conn)) == 1
    spec = read_sources(service.home)['migrated-ops-task']
    assert spec['profile'] == 'aiyu-ops' and spec['session_key'] == 'ops-task'
