"""Official intake resolves persisted native provenance, with no per-task policy."""
import json

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, projects_db
from hermes_cli import kanban_source
from hermes_cli.kanban_task_source import read_sources
from hermes_state import SessionDB


@pytest.fixture
def intake(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    home = tmp_path / '.hermes'; home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    kb.create_board('registry', tracking_only=True)
    with projects_db.connect_closing() as conn:
        project = projects_db.create_project(conn, name='Native project', slug='system', primary_path=str(tmp_path))
    source = SessionSource(Platform.DISCORD, '100000000000000002', chat_type='thread',
        user_id='owner', thread_id='100000000000000002', parent_chat_id='parent', scope_id='guild')
    config = GatewayConfig(sessions_dir=home / 'sessions', multiplex_profiles=False, write_sessions_json=False)
    store = SessionStore(config.sessions_dir, config)
    entry = store.get_or_create_session(source)
    key = entry.session_key
    store.close_all_db_handles()
    policy = dict(enabled=True, coordination=dict(enabled=True, owner_user_id='owner', guild_id='guild',
        mains={'default': dict(session_key='main-one', session_id='m1', chat_id='parent'),
               'aiyu-ops': dict(session_key='main-two', session_id='m2', chat_id='ops-parent')},
        handoff_root=str(tmp_path / 'docs'), task_parents={'default': ['parent']}))
    (home / 'config.yaml').write_text(json.dumps({'agent_handoff': policy}))
    document = tmp_path / 'docs' / 'hermes' / 'example-research' / 'HANDOFF.md'
    document.parent.mkdir(parents=True); document.write_text('isolated cross-project contract fixture')
    args = ['accept', '--task-id', 'example-research', '--source-key', key,
            '--project', project, '--handoff-path', str(document), '--title', 'Native task']
    return home, project, source, entry, args, document


def test_official_accept_once_without_records_policy_or_task_list(intake, capsys, monkeypatch):
    import argparse
    from hermes_cli.kanban_parser import build_parser
    from hermes_cli.kanban import kanban_command
    from scripts.agent_task_intake import main as edge_main
    home, project, _source, _entry, args, _doc = intake
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest='command'))
    assert kanban_command(parser.parse_args(['kanban', 'source', *args])) == 0
    first = json.loads(capsys.readouterr().out)
    assert edge_main(args) == 0
    assert json.loads(capsys.readouterr().out)['native_id'] == first['native_id']
    spec = read_sources(home)['example-research']
    assert spec['project_id'] == project
    assert spec['document_project'] == 'hermes'
    assert spec['session_id'] == _entry.session_id
    with kbc.connect_closing(board='registry') as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert kb.get_task(conn, first['native_id']).status == 'running'
        assert kb.list_runs(conn, first['native_id']) == []
        assert kb.claim_task(conn, first['native_id']) is None
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
    monkeypatch.setattr('hermes_cli.kanban_db_dispatch._default_spawn',
                        lambda *a, **kw: pytest.fail('tracking board attempted native spawn'))
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({'default_assignee': 'default'}, kb))
    dispatched = dispatcher.tick_once_for_board('registry')
    assert not dispatched.spawned and dispatched.promoted == 0
    assert 'tasks' not in json.loads((home / 'config.yaml').read_text())['agent_handoff']['coordination']


@pytest.mark.parametrize('problem', ['missing-route', 'forged-sid', 'profile', 'owner', 'guild', 'parent',
                                   'missing-state', 'wrong-key', 'bad-document', 'bad-project', 'unreadable-state'])
def test_intake_cannot_promote_claimed_identity_to_authority(intake, capsys, problem):
    home, project, source, entry, args, doc = intake
    if problem == 'bad-document':
        args[args.index(str(doc))] = str(doc.parent / 'HTTPS-DEPENDENCY.md')
    elif problem == 'bad-project':
        args[args.index(project)] = 'not-a-project'
    elif problem == 'unreadable-state':
        (home / 'state.db').rename(home / 'state.db.saved')
    else:
        with SessionDB(home / 'state.db') as state:
            if problem == 'missing-route':
                state.replace_gateway_routing_entries({}, scope=str((home / 'sessions').resolve()))
            elif problem == 'missing-state':
                state.end_session(entry.session_id, 'manual')
            else:
                if problem == 'forged-sid':
                    entry.session_id = 'claim-only'
                elif problem == 'wrong-key':
                    entry.session_key = 'claimed-key'
                else:
                    field = {'profile': 'profile', 'owner': 'user_id', 'guild': 'scope_id', 'parent': 'parent_chat_id'}[problem]
                    setattr(source, field, 'attacker')
                state.save_gateway_routing_entry(args[args.index('--source-key') + 1], json.dumps(entry.to_dict()),
                                                 scope=str((home / 'sessions').resolve()))
    assert kanban_source.main(args) == 2
    assert 'error' in json.loads(capsys.readouterr().out)
    with kbc.connect_closing(board='registry') as conn:
        assert kb.list_tasks(conn) == []


def test_observations_use_card_and_preserve_manual_state_and_acceptance(intake, capsys, tmp_path):
    _home, _, _, entry, args, _ = intake
    assert kanban_source.main(args) == 0
    tid = json.loads(capsys.readouterr().out)['native_id']
    event = dict(task_id='example-research', kind='progress', revision=1, summary='Updated owner metadata',
                 owner='worker-model-stage-changed', observed_at='2026-10-31T00:00:00+00:00', needs_user=False)
    ep = tmp_path / 'event.json'
    observe = ['observe', '--task-id', event['task_id'], '--source-key', entry.session_key, '--event', str(ep)]
    for kind, status in [('progress', 'running'), ('idle', 'blocked'), ('completed', 'review')]:
        event['kind'] = kind; event['revision'] += 1; ep.write_text(json.dumps(event))
        assert kanban_source.main(observe) == 0
        assert json.loads(capsys.readouterr().out)['status'] == status
    with kbc.connect_closing(board='registry') as conn:
        assert kb.observe_tracking_task(conn, tid, 'blocked', actor='user', evidence={'reason': 'User pause'})
    event.update(kind='progress', revision=5); ep.write_text(json.dumps(event))
    assert kanban_source.main(observe) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'blocked'
    assert kanban_source.main(observe) == 0
    capsys.readouterr()
    event.update(kind='accepted', revision=6); ep.write_text(json.dumps(event))
    assert kanban_source.main(observe) == 2
    capsys.readouterr()
    with kbc.connect_closing(board='registry') as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert kb.list_runs(conn, tid) == []


def test_existing_card_migration_uses_same_native_id_without_body_authority(intake, capsys):
    from scripts.kanban_registry_records import task_key
    _, project, _, _, args, _ = intake
    with kbc.connect_closing(board='registry') as conn:
        tid = kb.create_task(conn, title='Original Xiaoyi card fixture', project_id=project, board='registry',
                             initial_status='blocked', body='Untrusted text and comments are not admission',
                             idempotency_key=task_key(dict(profile='default', task_id='example-research')))
        kb.edit_task(conn, tid, priority=9)
    assert kanban_source.main([*args, '--native-id', tid]) == 0
    assert json.loads(capsys.readouterr().out)['native_id'] == tid
    with kbc.connect_closing(board='registry') as conn:
        task = kb.get_task(conn, tid)
        assert task.priority == 9 and task.status == 'blocked'
        assert task.body == 'Untrusted text and comments are not admission'
        assert len(kb.list_tasks(conn)) == 1


@pytest.mark.parametrize('guard', ['HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB', 'HERMES_DELEGATED_CHILD_CONTEXT'])
def test_operator_entry_preserves_worker_and_db_fences(intake, capsys, monkeypatch, guard):
    *_, args, _document = intake
    monkeypatch.setenv(guard, 'explicit-worker-pin')
    assert kanban_source.main(args) == 2
    assert 'refused' in json.loads(capsys.readouterr().out)['error']


def test_concurrent_acceptance_converges_without_extra_cards(intake):
    from concurrent.futures import ThreadPoolExecutor
    from hermes_cli.kanban_source import execute
    from types import SimpleNamespace
    home, project, _, entry, _, document = intake
    args = SimpleNamespace(command='accept', task_id='example-research', source_key=entry.session_key,
                           project=project, native_id=None, handoff_path=str(document), title='Task', event=None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(args), range(2)))
    assert len({r['native_id'] for r in results}) == 1
    assert len(read_sources(home)) == 1
    with kbc.connect_closing(board='registry') as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert kb.list_runs(conn, results[0]['native_id']) == []


def test_native_mode_legacy_intake_and_registry_write_are_retired(intake, capsys, tmp_path):
    from scripts.agent_task_intake import prepare
    from scripts.kanban_registry_records import RegistryError
    from scripts.kanban_registry_sync import main as registry_main
    policy = tmp_path / 'old-policy.json'
    policy.write_text(json.dumps({'mode': 'native'}), encoding='utf-8')
    with pytest.raises(RegistryError, match='legacy_intake_retired'):
        prepare({'registry_config': str(policy)}, {})
    # A valid archived config/records fixture exercises the public CLI retirement.
    from unittest.mock import patch
    with patch('scripts.kanban_registry_sync.load_config', return_value={'mode': 'native'}), \
            patch('scripts.kanban_registry_sync.load_records', return_value=[]):
        assert registry_main(['--config', str(policy), '--records', str(policy), 'sync']) == 2
    assert json.loads(capsys.readouterr().out)['error'] == 'legacy_registry_writes_retired_use_kanban_source'
