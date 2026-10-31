"""Offline repair: real native guards + real UDS transport; no live identity or provider calls."""

import asyncio
import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from gateway import agent_handoff
from gateway.agent_coordination import coordination_policy, caller_endpoint, submit_document
from gateway.control_socket import GatewayControlServer
from gateway.session import SessionSource
from scripts import task_handoff_check as check
from scripts import task_handoff_host as host
from scripts import task_handoff_native as native
from tests.scripts import test_task_handoff_check as fixture_module
from tests.waiting import async_wait_until

pytestmark = pytest.mark.platforms('posix')


@pytest.fixture(name='task')
def checker_fixture(tmp_path, monkeypatch):
    # Native AF_UNIX endpoint stays in its original short, isolated owning home.
    with tempfile.TemporaryDirectory(prefix='hh-', dir='/tmp') as directory:  # no-tmp: ok -- AF_UNIX sun_path limit
        yield fixture_module.task.__wrapped__(Path(directory).resolve(), monkeypatch)


@pytest.fixture
def actual(task, monkeypatch):
    # Same offline fixture as the maintained collector suite, upgraded to the
    # actual installed native_endpoint/caller/edge guards. Transport is simulated.
    service = task.bridge.service
    policy = task.bridge.policy
    policy.update(enabled=True, owner_user_id='400', guild_id='300', handoff_root=str(task.spec.handoff_root),
                  task_parents={'default': ['100']})
    policy['mains']['ops'] = {'session_key': 'ops-main', 'session_id': 'ops-main-sid', 'chat_id': '999'}
    service.policy['enabled'] = True
    service.coordination_lock = asyncio.Lock()
    entries = {}
    for name, source in task.bridge.sources.items():
        key = 'task-key' if name.startswith('task:') else 'main-key'
        entries[key] = SimpleNamespace(origin=SessionSource.from_dict(source), session_id=task.bridge.sids[name])
    async def lookup(key):
        return entries.get(key)
    async def list_sessions():
        return list(entries.values())
    runner = service.runner
    runner.async_session_store = SimpleNamespace(lookup_by_session_key=lookup, list_sessions=list_sessions)
    runner._restored_source = lambda entry: entry.origin
    runner._session_key_for_source = lambda source: 'task-key' if source.chat_type == 'thread' else 'main-key'
    runner._is_user_authorized_for_source = lambda source, **kwargs: True
    class Adapter:
        pass
    adapter = Adapter()
    runner._delivery_adapter_for = lambda source: adapter
    runner._canonicalize = lambda source: SimpleNamespace(runtime_profile='default')
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, projects_db
    from hermes_cli.kanban_task_source import accept_task
    monkeypatch.setenv('HERMES_HOME', str(task.home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(task.home))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    kb.create_board('registry', tracking_only=True)
    with projects_db.connect_closing() as conn:
        project = projects_db.create_project(conn, name='System', slug='system', primary_path=str(task.home))
    with kbc.connect_closing(board='registry') as conn:
        accept_task(conn, policy=policy, source=entries['task-key'].origin, session_key='task-key',
            session_id='task-sid', profile_home=task.home, task_id='sample-task',
            document_project='system', project_id=project, title='Offline sample')
    policy.pop('tasks')
    monkeypatch.setattr(agent_handoff, '_service', service)
    # Recover the real bridge implementation, rather than constructing a fake
    # production service or assigning a caller ContextVar.
    def real_bridge():
        from gateway.agent_coordination import coordination_policy, native_endpoint, caller_endpoint, target_endpoint
        return SimpleNamespace(service=service, policy=coordination_policy(service), native_endpoint=native_endpoint,
                               caller_endpoint=caller_endpoint, target_endpoint=target_endpoint)
    monkeypatch.setattr(native, 'runtime_bridge', real_bridge)
    data = asdict(task.spec)
    for key in ('home', 'handoff_root', 'document'):
        data[key] = str(data[key])
    descriptor = dict(native_revision='offline-native-pin', allow_registration=True,
                      task={k: data[k] for k in host.BINDINGS},
                      scope_body_sha256=host.sha(task.spec.message_body.encode()))
    task.entries, task.descriptor = entries, descriptor
    task.host = host.Host(service, descriptor)
    return task


def test_card_source_replaces_old_config_registration(actual):
    service = actual.bridge.service
    assert 'tasks' not in service.policy['coordination']
    before = host.sha((actual.home / 'runtime/agent_handoff.db').read_bytes())
    derived = coordination_policy(service)
    assert caller_endpoint(derived, 'task-key') == 'task:sample-task'
    assert derived['tasks']['sample-task']['native_id']
    with pytest.raises(PermissionError, match='retired'):
        asyncio.run(actual.host.registration_plan())
    assert host.sha((actual.home / 'runtime/agent_handoff.db').read_bytes()) == before


@pytest.mark.parametrize('operation', ['plan', 'activate'])
def test_retired_cli_never_queries_host_or_config(actual, monkeypatch, capsys, operation):
    monkeypatch.setattr(host, 'query', lambda *a, **k: pytest.fail('retired writer contacted host'))
    before = copy.deepcopy(actual.bridge.service.policy)
    argv = ['--home', str(actual.home), operation]
    if operation == 'activate':
        argv += ['--expected-config-sha256', 'untrusted']
    assert host.main(argv) == 2
    assert json.loads(capsys.readouterr().out)['state'] == 'retired'
    assert actual.bridge.service.policy == before
    assert not (actual.home / 'config.yaml').exists()


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'owner', 'guild', 'canonical', 'auth', 'reset'])
def test_current_card_source_uses_real_native_guards(actual, defect):
    service = actual.bridge.service
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_task_source import read_sources, SOURCE_EVENT
    if defect == 'duplicate':
        spec = read_sources(actual.home)['sample-task']
        with kbc.connect_closing(board='registry') as conn:
            kb._append_event(conn, spec['native_id'], SOURCE_EVENT, dict(spec, task_id='duplicate'))
            conn.commit()
    else:
        changes = {
            'missing': lambda: actual.entries.pop('task-key'),
            'owner': lambda: setattr(actual.entries['task-key'].origin, 'user_id', '999'),
            'guild': lambda: setattr(actual.entries['task-key'].origin, 'scope_id', '999'),
            'canonical': lambda: setattr(service.runner, '_canonicalize', lambda source: SimpleNamespace(runtime_profile='other')),
            'auth': lambda: setattr(service.runner, '_is_user_authorized_for_source', lambda *a, **k: False),
            'reset': lambda: setattr(actual.entries['task-key'], 'session_id', 'reset-without-native-session'),
        }
        changes[defect]()
    with pytest.raises((PermissionError, KeyError)):
        asyncio.run(submit_document(service, 'task-key', 'task-sid', actual.home,
                                    'repair-probe-01', 'sample-task', str(actual.spec.document), 'main'))
    assert 'tasks' not in service.policy['coordination']


@pytest.mark.parametrize('defect', ['hash', 'other_policy', 'wrong_task', 'not_approved'])
def test_retired_activation_never_reads_or_writes_config(actual, defect):
    service = actual.bridge.service
    config = copy.deepcopy(service.policy)
    changes = {'hash': lambda: None, 'other_policy': lambda: config.update(execution_enabled=False),
               'wrong_task': lambda: config['coordination'].update(tasks={'sample-task': {'session_key': 'other-key'}}),
               'not_approved': lambda: actual.descriptor.update(allow_registration=False)}
    changes[defect]()
    path = actual.home / 'config.yaml'
    path.write_text(json.dumps({'agent_handoff': config}), encoding='utf-8')
    before, policy = path.read_bytes(), copy.deepcopy(service.policy)
    with pytest.raises(PermissionError, match='retired'):
        asyncio.run(actual.host.activate('wrong-hash' if defect == 'hash' else host.sha(before)))
    assert path.read_bytes() == before and service.policy == policy


@pytest.mark.asyncio
async def test_retired_activation_does_not_touch_concurrent_native_endpoints(actual, monkeypatch):
    from gateway import agent_coordination
    service = actual.bridge.service
    service.policy['concurrent_change'] = 'preserve'
    monkeypatch.setattr(agent_coordination, 'native_endpoint',
                        lambda *a: pytest.fail('retired activation resolved a native endpoint'))
    with pytest.raises(PermissionError, match='retired'):
        await actual.host.activate('untrusted')
    assert service.policy['concurrent_change'] == 'preserve'


@pytest.mark.asyncio
async def test_whole_check_cannot_cross_task_or_accept_replayed_body(actual):
    payload = asdict(actual.spec)
    payload['native_revision'] = 'offline-native-pin'
    payload['thread'] = '999'
    with pytest.raises(PermissionError):
        await actual.host.check(payload)
    payload['thread'] = actual.spec.thread
    payload['message_body'] = 'Fabricated statement that work passed.'
    with pytest.raises(PermissionError):
        await actual.host.check(payload)


@pytest.mark.parametrize('defect', ['ended', 'owner', 'profile', 'key', 'missing-store'])
def test_read_only_collector_rechecks_actual_session_rows_without_static_tasks(actual, defect):
    from hermes_state import SessionDB
    if defect == 'missing-store':
        (actual.home / 'state.db').rename(actual.home / 'saved-state.db')
    else:
        with SessionDB(actual.home / 'state.db') as db:
            if defect == 'ended':
                db.end_session('task-sid', 'session_reset')
            else:
                field = {'owner': 'user_id', 'profile': 'profile_name', 'key': 'session_key'}[defect]
                db._write_sql(f'UPDATE sessions SET {field}=? WHERE id=?', ('forged', 'task-sid'))
    assert asyncio.run(check.check_task(actual.spec))['status'] == 'FAIL'
    assert 'tasks' not in actual.bridge.service.policy['coordination']


@pytest.mark.asyncio
async def test_client_has_no_saved_pass_fallback_and_exec_is_blocked(task):
    task.spec.native_host = True
    marker = task.home / 'must-not-exist'
    result, code = await check.guard(task.spec, [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()'])
    assert code == 3 and result['exec']['started'] is False
    assert not marker.exists()


@pytest.mark.asyncio
async def test_real_socket_fresh_process_host_and_native_collector(actual):
    home = actual.home
    identity = dict(pid=os.getpid(), start_time=1, code_sha='offline-native-pin', hermes_home=str(home))
    control = GatewayControlServer(home, verb_handlers={'identify': lambda: identity})
    assert await control.start()
    server = host.Host(actual.bridge.service, actual.descriptor)
    running = asyncio.create_task(server.serve())
    try:
        assert await async_wait_until(lambda: host.socket_path(home).exists(), timeout=5)
        # Actual independent Python CLI, authenticated to this host process
        # via the normal gateway identify socket. No model or network.
        child = await asyncio.create_subprocess_exec(sys.executable, '-m', 'scripts.task_handoff_host',
                    '--home', str(home), 'status', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=10)
        assert child.returncode == 0, stderr
        assert json.loads(stdout)['pid'] == os.getpid()
        before = {p: host.sha(p.read_bytes()) for p in (home / 'state.db', home / 'runtime/agent_handoff.db')}
        actual.spec.native_host = True
        actual.spec.native_revision = 'offline-native-pin'
        result = await check.check_task(actual.spec)
        # Task home changed; update the in-memory native receipt association
        # and source home as above, never manufacture a live runtime receipt.
        assert result['status'] == 'PASS', result
        assert all(host.sha(p.read_bytes()) == value for p, value in before.items())
        marker = home / 'child-must-not-start'
        command = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()']
        # Same-user transport still must bind the actual owning process and
        # reviewed native revision; no earlier PASS is reused by exec.
        identity['start_time'] = 2
        result, code = await check.guard(actual.spec, command)
        assert code == 3 and result['exec']['started'] is False
        identity['start_time'] = 1
        actual.spec.native_revision = 'unreviewed'
        result, code = await check.guard(actual.spec, command)
        assert code == 3 and result['exec']['started'] is False
        actual.spec.native_revision = 'offline-native-pin'
        host.socket_path(home).chmod(0o666)
        result, code = await check.guard(actual.spec, command)
        assert code == 3 and result['exec']['started'] is False
        host.socket_path(home).chmod(0o600)
        actual.payloads['/channels/200/thread-members/400']['user_id'] = '999'
        result, code = await check.guard(actual.spec, command)
        assert result['status'] == 'FAIL' and code == 2
        assert result['exec']['started'] is False and not marker.exists()
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await control.stop()
    assert not host.socket_path(home).exists()
