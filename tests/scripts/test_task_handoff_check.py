"""Offline native-boundary fixtures; never a claim of real Discord model activation."""
import asyncio
import copy
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import task_handoff_check as check
from scripts import task_handoff_native as native

REAL_DISCORD_GET = check.discord_get


@pytest.fixture
def task(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    (home / 'runtime').mkdir(parents=True)
    document = tmp_path / 'handoffs' / 'system' / 'sample-task' / 'HANDOFF.md'
    document.parent.mkdir(parents=True)
    document.write_text('Bounded offline fixture task.\n')
    spec = check.Task('sample-task', 'system', 'default', home, document.parents[2],
                      document, '100', '200', '300', '400', '500', 'Substantive task scope.',
                      'request-0001', 'execution_started', 600)
    source = dict(platform='discord', profile='default', user_id='400', scope_id='300',
                  chat_id='200', thread_id='200', parent_chat_id='100', chat_type='thread')
    main = dict(source, chat_id='100', thread_id=None, parent_chat_id=None, chat_type='group')
    policy = {'mains': {'default': {'session_key': 'main-key', 'session_id': 'main-sid',
                                   'chat_id': '100', 'profile': 'default'}},
              'tasks': {'sample-task': {'session_key': 'task-key', 'session_id': 'task-sid',
                                       'chat_id': '200', 'parent_chat_id': '100',
                                       'project': 'system', 'profile': 'default'}}}
    record = dict(protocol=2, kind='coordination', request_id='request-0001',
                  root_request_id='request-0001', source_endpoint='main:default',
                  target_endpoint='task:sample-task', source_key='main-key', target_key='task-key',
                  source_sid='main-sid', target_sid='task-sid', source_profile='default',
                  target_profile='default', caller_home=str(home), origin=main, target=source,
                  document=check.document_pointer(spec), created_at=time.time() - 5,
                  updated_at=time.time(), request_admitted_at=time.time() - 4,
                  status='generated', target_message_ids=['600'], response='Offline result.')
    with sqlite3.connect(home / 'runtime' / 'agent_handoff.db') as db:
        db.execute('CREATE TABLE requests (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        db.execute('INSERT INTO requests VALUES (?, ?)', (record['request_id'], json.dumps(record)))
    # Real SessionDB schema and compression contract, isolated HOME from the runner.
    from hermes_state import SessionDB
    db = SessionDB(home / 'state.db')
    for sid, key, origin in [('task-sid', 'task-key', source), ('main-sid', 'main-key', main)]:
        db.create_session(sid, 'discord', session_key=key, origin_json=json.dumps(origin),
            profile_name=origin['profile'], user_id=origin['user_id'], chat_id=origin['chat_id'],
            chat_type=origin['chat_type'], thread_id=origin['thread_id'])
    db.close()
    envelope = {**record['document'], 'request_id': record['request_id'],
                'target_endpoint': record['target_endpoint'], 'return_session': 'main-key'}
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('INSERT INTO messages (session_id, role, content, timestamp, display_metadata) '
                   'VALUES (?, ?, ?, ?, ?)', ('task-sid', 'user', native.HEADER + '\n' + json.dumps(envelope),
                    time.time() - 3, json.dumps({'native_handoff_author': {'id': 'bot:default', 'is_bot': True}})))
        db.execute('INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?, ?, ?, ?)',
                   ('task-sid', 'assistant', json.dumps([{'id': 'call-1', 'function': {'name': 'read_file'}}]), time.time() - 2))
        db.execute('INSERT INTO messages (session_id, role, tool_call_id, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)',
                   ('task-sid', 'tool', 'call-1', 'read_file', time.time() - 1))
    service = SimpleNamespace(home=home, runner=SimpleNamespace(_running=True,
                              _resolve_profile_home_for_source=lambda source: home),
                              policy={'coordination': policy}, fixture=True)
    bridge = SimpleNamespace(service=service, policy=policy, sources={'main:default': main,
                             'task:sample-task': source}, sids={'main:default': 'main-sid', 'task:sample-task': 'task-sid'})
    async def endpoint(service, policy, name):
        return SimpleNamespace(**bridge.sources[name], to_dict=lambda: bridge.sources[name]), bridge.sids[name]
    bridge.native_endpoint = endpoint
    # Offline native API doubles; fixed-native producer/guard integration runs separately.
    def caller_endpoint(policy, source_key):
        bindings = [(f'main:{name}', value) for name, value in policy['mains'].items()]
        bindings += [(f'task:{name}', value) for name, value in policy['tasks'].items()]
        matches = [name for name, value in bindings if value.get('session_key') == source_key]
        if len(matches) != 1:
            raise PermissionError('caller must have one explicit session registration')
        return matches[0]
    def target_endpoint(policy, caller, target, task_id):
        if caller != 'main:' + policy['tasks'][task_id]['profile'] or target != 'task:' + task_id:
            raise PermissionError('main may contact only its own registered task')
        return target
    bridge.caller_endpoint, bridge.target_endpoint = caller_endpoint, target_endpoint
    monkeypatch.setattr(native, 'runtime_bridge', lambda: bridge)
    payloads = {
        '/channels/200': {'id': '200', 'type': 11, 'parent_id': '100', 'guild_id': '300',
                          'thread_metadata': {'archived': False, 'locked': False}},
        '/channels/200/thread-members/400': {'id': '200', 'user_id': '400'},
        '/channels/200/messages/500': {'id': '500', 'channel_id': '200', 'content': spec.message_body},
        '/channels/200/messages/600': {'id': '600', 'channel_id': '200', 'content': 'Offline result.'},
    }
    monkeypatch.setattr(check, 'discord_get', lambda task, path: copy.deepcopy(payloads[path]))
    return SimpleNamespace(spec=spec, payloads=payloads, record=record, bridge=bridge, home=home)


def run(task):
    return asyncio.run(check.check_task(task.spec))


def update(task, **changes):
    task.record.update(changes)
    with sqlite3.connect(task.home / 'runtime' / 'agent_handoff.db') as db:
        db.execute('UPDATE requests SET record=?', (json.dumps(task.record),))


def test_positive_is_repeatable_and_completed_needs_no_live_worker(task):
    update(task, status='delivered')
    first, second = run(task), run(task)
    assert first['status'] == second['status'] == 'PASS'
    assert first['stages']['execution_started'] == 'PASS'


@pytest.mark.parametrize('path,changes', [
    ('/channels/200', {'parent_id': '999'}), ('/channels/200', {'guild_id': '999'}),
    ('/channels/200', {'id': '999'}), ('/channels/200', {'type': 0}),
    ('/channels/200', {'thread_metadata': {'archived': True, 'locked': False}}),
    ('/channels/200', {'thread_metadata': {'archived': False, 'locked': True}}),
    ('/channels/200/thread-members/400', {'user_id': '999'}),
    ('/channels/200/messages/500', {'channel_id': '100'}),
    ('/channels/200/messages/500', {'content': 'https://example.com/HANDOFF.md'}),
    ('/channels/200/messages/500', {'content': ''}),
])
def test_wrong_or_empty_thread_fails(task, path, changes):
    task.payloads[path].update(changes)
    assert run(task)['status'] == 'FAIL'


@pytest.mark.parametrize('stage', ['handoff_received', 'execution_started', 'result_routed'])
def test_no_native_service_never_trusts_worker_or_verified_json(task, monkeypatch, stage):
    task.spec.stage = stage
    (task.home / 'CONTROLLER-STATUS.json').write_text(json.dumps({'verified': True, 'owner_status': 'running'}))
    monkeypatch.setattr(native, 'runtime_bridge', lambda: None)
    result = run(task)
    assert result['stages']['thread_ready'] == 'PASS'
    assert result['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('changes', [
    {'target_key': 'main-key'}, {'target_sid': 'main-sid'}, {'target_endpoint': 'main:default'},
    {'source_profile': 'wrong'}, {'target': {'chat_id': '100'}},
    {'document': {'sha256': 'forged'}}, {'protocol': 1},
])
def test_wrong_native_association_fails(task, changes):
    update(task, **changes)
    assert run(task)['status'] == 'FAIL'


@pytest.mark.parametrize('change', ['missing', 'unreadable', 'stale', 'queued', 'only_document', 'main_tools'])
def test_missing_stale_or_wrong_execution_evidence(task, change):
    if change == 'missing':
        (task.home / 'runtime' / 'agent_handoff.db').unlink()
    if change == 'unreadable':
        (task.home / 'runtime' / 'agent_handoff.db').write_bytes(b'not sqlite')
    if change == 'stale':
        update(task, updated_at=time.time() - 700)
    if change == 'queued':
        update(task, status='queued', request_admitted_at=None)
    if change in {'only_document', 'main_tools'}:
        with sqlite3.connect(task.home / 'state.db') as db:
            if change == 'only_document':
                db.execute("DELETE FROM messages WHERE role != 'user'")
            else:
                db.execute("UPDATE messages SET session_id='main-sid' WHERE role != 'user'")
    assert run(task)['status'] == 'UNVERIFIED'


def test_phase_requirements_and_no_implicit_completion(task):
    with sqlite3.connect(task.home / 'state.db') as db:
        db.execute("DELETE FROM messages WHERE role != 'user'")
    task.spec.stage = 'handoff_received'
    assert run(task)['status'] == 'PASS'
    task.spec.stage = 'execution_started'
    assert run(task)['status'] == 'UNVERIFIED'
    task.spec.stage = 'result_routed'
    assert run(task)['status'] == 'UNVERIFIED'


def test_result_must_be_trusted_target_delivery_and_read_back_exactly(task):
    task.spec.stage = 'result_routed'
    assert run(task)['status'] == 'PASS'
    task.payloads['/channels/200/messages/600']['channel_id'] = '100'
    assert run(task)['status'] == 'FAIL'


def test_missing_fields_and_permission_errors_are_unverified(task, monkeypatch):
    task.payloads['/channels/200']['thread_metadata'] = {}
    assert run(task)['status'] == 'UNVERIFIED'
    def denied(*args):
        raise PermissionError('secret must never appear in output')
    monkeypatch.setattr(check, 'discord_get', denied)
    result = run(task)
    assert result['status'] == 'UNVERIFIED'
    assert 'secret must' not in json.dumps(result)


def test_guard_never_runs_nonzero_and_pass_preserves_literal_argv(task, tmp_path):
    marker = tmp_path / 'marker'
    command = [sys.executable, '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
               str(marker), '$(touch INJECTION); `false`']
    task.payloads['/channels/200']['parent_id'] = '999'
    result, code = asyncio.run(check.guard(task.spec, command))
    assert code != 0 and not marker.exists()
    task.payloads['/channels/200']['parent_id'] = '100'
    result, code = asyncio.run(check.guard(task.spec, command))
    assert code == 0 and marker.read_text() == command[-1]
    assert result['status'] == 'PASS'


def test_guard_unverified_blocks_and_exit_status_is_preserved(task, monkeypatch):
    monkeypatch.setattr(native, 'runtime_bridge', lambda: None)
    _, code = asyncio.run(check.guard(task.spec, [sys.executable, '-c', 'raise SystemExit(0)']))
    assert code == 3
    task.spec.stage = 'thread_ready'
    _, code = asyncio.run(check.guard(task.spec, [sys.executable, '-c', 'raise SystemExit(17)']))
    assert code == 17


@pytest.mark.parametrize('kind,accepted', [('compression', True), ('reset', False), ('branch', False)])
def test_sid_continuation_uses_real_native_compression_contract(task, kind, accepted):
    from hermes_state import SessionDB
    db = SessionDB(task.home / 'state.db')
    db.create_session('next-sid', 'discord', parent_session_id='task-sid', session_key='task-key',
                      model_config={'_branched_from': 'task-sid'} if kind == 'branch' else {})
    db.end_session('task-sid', kind if kind != 'branch' else 'compression')
    db.close()
    task.bridge.sids['task:sample-task'] = 'next-sid'
    assert (run(task)['status'] == 'PASS') is accepted


def test_document_symlink_and_changed_version_fail(task, tmp_path):
    task.spec.document.write_text('changed')
    assert run(task)['status'] == 'FAIL'
    other = tmp_path / 'other'
    other.write_text('changed')
    task.spec.document.unlink()
    task.spec.document.symlink_to(other)
    assert run(task)['status'] == 'FAIL'


@pytest.mark.parametrize('status', [None, 'invented_verified', 'accepted', 'queued'])
def test_unknown_or_nonexecution_receipt_state_never_authorizes(task, status):
    update(task, status=status, verified=True)
    assert run(task)['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('author', [{}, {'id': '400', 'is_bot': False}, {'id': 'bot:other', 'is_bot': True}])
def test_bot_post_or_text_envelope_without_native_author_is_insufficient(task, author):
    with sqlite3.connect(task.home / 'state.db') as db:
        db.execute("UPDATE messages SET display_metadata=? WHERE role='user'",
                   (json.dumps({'native_handoff_author': author}),))
    assert run(task)['status'] == 'UNVERIFIED'


def test_journal_view_and_symlink_are_not_native_store(task, tmp_path):
    journal = task.home / 'runtime' / 'agent_handoff.db'
    with sqlite3.connect(journal) as db:
        db.execute('ALTER TABLE requests RENAME TO backing')
        db.execute('CREATE VIEW requests AS SELECT * FROM backing')
    assert run(task)['status'] == 'UNVERIFIED'
    other = tmp_path / 'other.db'
    journal.rename(other)
    journal.symlink_to(other)
    assert run(task)['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('age', [float('inf'), float('nan'), -1, 86401])
def test_no_stale_evidence_bypass_through_library_api(task, age):
    task.spec.max_age = age
    with pytest.raises(ValueError):
        run(task)


def test_cli_has_no_force_or_saved_status_input(capsys):
    code = check.main(['exec', '--force', '--verified', 'true', '--', 'never-run'])
    assert code == 3
    assert json.loads(capsys.readouterr().out)['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('raises', [False, True])
def test_official_secret_scope_uses_selected_profile_and_always_resets(task, monkeypatch, raises):
    from agent import secret_scope
    from tools import discord_tool
    monkeypatch.setenv('DISCORD_BOT_TOKEN', 'OFFLINE_AMBIENT_TOKEN')
    monkeypatch.setattr(secret_scope, 'build_profile_secret_scope',
                        lambda home: {'DISCORD_BOT_TOKEN': 'OFFLINE_PROFILE_TOKEN'})
    seen = []
    def request(method, path, token, timeout):
        seen.append((method, path, token))
        if raises:
            raise OSError('OFFLINE_PRIVATE_EXCEPTION_BODY')
        return {'id': '200'}
    monkeypatch.setattr(discord_tool, '_discord_request', request)
    outer = secret_scope.set_secret_scope({'outer': 'OFFLINE_OUTER'}, profile_home=str(task.home))
    try:
        if raises:
            with pytest.raises(OSError):
                REAL_DISCORD_GET(task.spec, '/channels/200')
        else:
            assert REAL_DISCORD_GET(task.spec, '/channels/200') == {'id': '200'}
        assert secret_scope.current_secret_scope() == {'outer': 'OFFLINE_OUTER'}
    finally:
        secret_scope.reset_secret_scope(outer)
    assert seen == [('GET', '/channels/200', 'OFFLINE_PROFILE_TOKEN')]


def test_missing_profile_secret_never_borrows_ambient_token(task, monkeypatch):
    from agent import secret_scope
    monkeypatch.setenv('DISCORD_BOT_TOKEN', 'OFFLINE_AMBIENT_TOKEN')
    monkeypatch.setattr(secret_scope, 'build_profile_secret_scope', lambda home: {})
    with pytest.raises(PermissionError):
        REAL_DISCORD_GET(task.spec, '/channels/200')


def test_guard_timeout_is_distinct_from_gate_status(task):
    task.spec.exec_timeout = 0.1
    result, code = asyncio.run(check.guard(task.spec,
        [sys.executable, '-c', 'import threading; threading.Event().wait()']))
    assert code == 124 and result['status'] == 'PASS'
    assert result['exec'] == {'started': True, 'timed_out': True, 'returncode': 124}


def test_absent_expected_body_cannot_pass_readiness(task):
    task.spec.message_body = None
    assert run(task)['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('defect', ['caller-alias', 'edge-rejected'])
def test_current_native_source_and_edge_must_authorize_guard(task, tmp_path, monkeypatch, defect):
    # B1: endpoint identities/receipt/tools remain valid after current policy drifts.
    assert run(task)['status'] == 'PASS'
    if defect == 'caller-alias':
        task.bridge.policy['tasks']['alias-task'] = dict(task.bridge.policy['tasks'][task.spec.task_id],
                                                        session_key='main-key')
    else:
        def refused(*args):
            raise PermissionError('current native edge refused')
        monkeypatch.setattr(task.bridge, 'target_endpoint', refused)
    stores = [task.home / 'state.db', task.home / 'runtime/agent_handoff.db']
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in stores}
    result = run(task)
    assert result['status'] == 'FAIL' and result['stages']['thread_ready'] == 'PASS'
    marker = tmp_path / 'must-not-execute'
    guarded, code = asyncio.run(check.guard(task.spec, [sys.executable, '-c',
        'from pathlib import Path;import sys;Path(sys.argv[1]).touch()', str(marker)]))
    assert guarded['status'] == 'FAIL' and code == 2 and not marker.exists()
    assert guarded['exec']['started'] is False
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
