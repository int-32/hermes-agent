"""Ad367 review reproducers through real SessionStore and native consumers."""
import json

from tests.gateway.test_agent_handoff import native_owner

import pytest

from gateway.agent_coordination import submit_document
from gateway.coordination_receipts import DOCUMENT_TURN, DocumentTurn, complete_turn
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, projects_db
from hermes_cli.kanban_task_source import accept_task, persisted_source, read_sources, SOURCE_EVENT
from hermes_state import SessionDB
from tests.gateway.native_source_fixtures import produced_star

pytestmark = pytest.mark.platforms('posix')


@pytest.fixture
def produced(tmp_path, monkeypatch):
    value = produced_star(tmp_path, monkeypatch, implicit=True, custom_scope=True)
    yield value
    value.store.close_all_db_handles()


async def submit(value, request_id='review-request-001', target='task:task-one'):
    task = target.partition(':')[2]
    origin = value.entries['ops-main' if task == 'ops-one' else 'default-main']
    path = value.path.parent.parent / task / 'HANDOFF.md'
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        path.write_text('Isolated named-profile request')
    home = value.service.runner._resolve_profile_home_for_source(origin.origin)
    return await submit_document(value.service, origin.session_key, origin.session_id, home,
                                  request_id, task, str(path), target)


@pytest.mark.parametrize('profile', ['default', 'aiyu-ops'])
def test_L1_real_session_store_producer_is_consumable(produced, profile):
    entry = produced.entries['default-task' if profile == 'default' else 'ops-task']
    with SessionDB(produced.home / 'state.db', read_only=True) as db:
        assert entry.session_key in db.load_gateway_routing_entries(scope=str(produced.config.sessions_dir.resolve()))
        assert entry.session_key not in db.load_gateway_routing_entries()
    source, sid, home = persisted_source(produced.home, entry.session_key, produced.service.policy['coordination'])
    assert sid == entry.session_id and (source.profile or 'default') == profile
    assert home == produced.service.runner._resolve_profile_home_for_source(source)


def test_L2_implicit_mode_follows_real_producer_namespace(produced):
    from gateway.config import GatewayConfig
    raw = json.loads((produced.home / 'config.yaml').read_text())
    assert GatewayConfig.from_dict(raw).multiplex_profiles is None
    assert produced.config.multiplex_profiles is True
    entry = produced.entries['ops-task']
    assert entry.session_key.startswith('agent:aiyu-ops:')
    source, sid, _ = persisted_source(produced.home, entry.session_key, produced.service.policy['coordination'])
    assert source.profile == 'aiyu-ops' and sid == entry.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize('consumer', ['request', 'queued', 'return', 'completion'])
@pytest.mark.parametrize('defect', ['ended', 'missing-store', 'owner', 'profile', 'key'])
@pytest.mark.parametrize('profile', ['default', 'aiyu-ops'])
async def test_S1_same_sid_cannot_replace_current_row_validation(produced, consumer, defect, profile):
    entry = produced.entries['default-task' if profile == 'default' else 'ops-task']
    target = 'task:task-one' if profile == 'default' else 'task:ops-one'
    if consumer != 'request':
        await submit(produced, target=target)
    home = produced.service.runner._resolve_profile_home_for_source(entry.origin)
    if defect == 'missing-store':
        produced.store.close_all_db_handles()
        (home / 'state.db').rename(home / 'state.db.saved')
    else:
        with SessionDB(home / 'state.db') as db:
            if defect == 'ended':
                db.end_session(entry.session_id, 'session_reset')
            else:
                field = {'owner': 'user_id', 'profile': 'profile_name', 'key': 'session_key'}[defect]
                db._write_sql(f'UPDATE sessions SET {field}=? WHERE id=?', ('forged', entry.session_id))
    # The real store deliberately still has its cached routing entry with the same SID.
    assert (await produced.service.runner.async_session_store.lookup_by_session_key(entry.session_key)).session_id == entry.session_id
    if consumer == 'request':
        with pytest.raises((PermissionError, OSError)):
            await submit(produced, target=target)
        assert produced.service.journal.get('review-request-001') is None
    elif consumer == 'completion':
        record = produced.service.journal.get('review-request-001')
        token = DOCUMENT_TURN.set(DocumentTurn(record, entry.session_key, entry.session_id, 'request'))
        try:
            with pytest.raises((PermissionError, OSError)):
                await complete_turn(produced.service, entry.session_key, entry.session_id, home, record['request_id'], 'completed')
        finally:
            DOCUMENT_TURN.reset(token)
    else:
        if consumer == 'return':
            produced.service.journal.update('review-request-001', status='generated', response='pinned result', outcome='completed')
        await produced.service.tick()
        assert produced.service.journal.get('review-request-001')['status'] == 'failed'
        assert not produced.service.runner.received


def test_L3_same_native_card_refuses_second_binding(produced):
    spec = read_sources(produced.home)['task-one']
    entry = produced.entries['ops-task']
    # Use a fresh legitimate source, not one already bound to ops-one.
    source = type(entry.origin).from_dict(entry.origin.to_dict())
    source.chat_id = source.thread_id = 'ops-second-task'
    entry = produced.store.get_or_create_session(source)
    with kbc.connect_closing(board='registry') as conn:
        card = kb.create_task(conn, title='plain existing card', board='registry', project_id=spec['project_id'])
        first = produced.entries['default-task']
        # The first real source is already bound, so create another legitimate source.
        first_source = type(source).from_dict(first.origin.to_dict())
        first_source.chat_id = first_source.thread_id = 'default-second-task'
        first_entry = produced.store.get_or_create_session(first_source)
        args = dict(policy=produced.service.policy['coordination'], project_id=spec['project_id'], native_id=card)
        accept_task(conn, **args, source=first_source, session_key=first_entry.session_key,
                    session_id=first_entry.session_id, profile_home=produced.home,
                    task_id='first-alias', document_project='project')
        with pytest.raises(PermissionError, match='card.*bound|card.*binding'):
            accept_task(conn, **args, source=source, session_key=entry.session_key,
                session_id=entry.session_id, profile_home=produced.home / 'profiles' / 'aiyu-ops',
                task_id='second-alias', document_project='other-project')


def test_L3_read_index_refuses_historical_double_binding(produced):
    spec = read_sources(produced.home)['task-one']
    with kbc.connect_closing(board='registry') as conn:
        second = dict(spec, task_id='forged-alias', session_key='another-real-key', document_project='other')
        kb._append_event(conn, spec['native_id'], SOURCE_EVENT, second)
        conn.commit()
        with pytest.raises(PermissionError, match='card|binding'):
            read_sources(produced.home)


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['paused', 'done', 'archived', 'project-archived', 'open'])
@pytest.mark.parametrize('phase', ['queued', 'generated'])
async def test_L4_legacy_receipt_respects_current_admission_and_return(produced, state, phase):
    await submit(produced)
    record = produced.service.journal.update('review-request-001', task_binding=None)
    spec = read_sources(produced.home)['task-one']
    with kbc.connect_closing(board='registry') as conn:
        if state == 'paused':
            assert kb.block_task(conn, spec['native_id'], reason='User pause')
        if state in {'done', 'archived'}:
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (state, spec['native_id'])); conn.commit()
    if state == 'project-archived':
        with projects_db.connect_closing() as conn:
            assert projects_db.archive_project(conn, spec['project_id'])
    if phase == 'generated':
        produced.service.journal.update(record['request_id'], status='generated', response='existing native result', outcome='completed')
    await produced.service.tick()
    result = produced.service.journal.get(record['request_id'])
    if state != 'open' and phase == 'queued':
        assert result['status'] == 'failed'
        assert not produced.service.runner.received
    else:
        assert result['status'] == 'delivered', result
        assert produced.service.runner.received


@pytest.mark.asyncio
async def test_L5_actual_host_old_write_consumers_are_explicitly_retired(produced):
    from scripts.task_handoff_host import Host
    policy = produced.service.policy['coordination']
    task = dict(home=str(produced.home), task_id='task-one', profile='default', project='project',
                document=str(produced.path), handoff_root=str(produced.path.parents[2]),
                guild=policy['guild_id'], user=policy['owner_user_id'], main_channel='default-main', thread='default-task')
    host = Host(produced.service, dict(task=task, allow_registration=True, native_revision='offline-pin'))
    before = json.dumps(produced.service.policy, sort_keys=True)
    with pytest.raises(PermissionError, match='retired'):
        await host.registration_plan()
    with pytest.raises(PermissionError, match='retired'):
        await host.activate('unused')
    status = await host.status()
    assert status['operations'] == ['status', 'check']
    assert json.dumps(produced.service.policy, sort_keys=True) == before


@pytest.mark.parametrize('mode', ['standalone', 'explicit', 'implicit'])
@pytest.mark.parametrize('custom_scope', [False, True])
def test_real_producer_scope_mode_and_official_cli_positive(tmp_path, monkeypatch, capsys, mode, custom_scope):
    from hermes_cli import kanban_source
    value = produced_star(tmp_path, monkeypatch, multiplex=mode != 'standalone',
                          implicit=mode == 'implicit', custom_scope=custom_scope)
    try:
        for chat, task in [('default-task', 'task-one'), ('ops-task', 'ops-one')]:
            if chat not in value.entries:
                continue
            entry = value.entries[chat]
            source, sid, home = persisted_source(value.home, entry.session_key, value.service.policy['coordination'])
            assert sid == entry.session_id and source.chat_id == chat
            assert home == value.service.runner._resolve_profile_home_for_source(source)
            document = value.path.parent.parent / task / 'HANDOFF.md'
            document.parent.mkdir(exist_ok=True)
            document.write_text('Isolated producer scope contract')
            args = ['accept', '--task-id', task, '--source-key', entry.session_key,
                    '--project', value.service.fixture_project_id, '--handoff-path', str(document)]
            assert kanban_source.main(args) == 0
            assert json.loads(capsys.readouterr().out)['native_id'] == read_sources(value.home)[task]['native_id']
    finally:
        value.store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle', ['open', 'agent_close', 'idle_timeout', 'compression-cached', 'compression-published'])
async def test_real_producer_legal_lifecycle_intake_request_private_completion_and_return(produced, lifecycle):
    entry = produced.entries['default-task']
    original = entry.session_id
    with SessionDB(produced.home / 'state.db') as db:
        if lifecycle.startswith('compression'):
            db.end_session(original, 'compression')
            db.create_session('real-compressed-child', 'discord', parent_session_id=original)
            if lifecycle == 'compression-published':
                entry.session_id = 'real-compressed-child'
                produced.store._save_entry(entry.session_key)
        elif lifecycle != 'open':
            db.end_session(original, lifecycle)
    source, sid, _ = persisted_source(produced.home, entry.session_key, produced.service.policy['coordination'])
    assert source.chat_id == entry.origin.chat_id and sid == entry.session_id
    assert (await submit(produced))['status'] == 'queued'
    await produced.service.tick()
    assert produced.service.journal.get('review-request-001')['status'] == 'delivered'
    assert len(produced.service.runner.received) == 2
    assert not produced.service.runner.sent


def test_standalone_named_profile_uses_real_launch_scope_and_legacy_namespace(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionSource, SessionStore
    value = produced_star(tmp_path, monkeypatch, multiplex=False)
    value.store.close_all_db_handles()
    home = value.home / 'profiles' / 'aiyu-ops'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    config = GatewayConfig(sessions_dir=home / 'sessions', multiplex_profiles=False, write_sessions_json=False)
    (home / 'config.yaml').write_text(json.dumps(config.to_dict()))
    store = SessionStore(config.sessions_dir, config)
    try:
        source = SessionSource.from_dict(value.entries['default-task'].origin.to_dict())
        source.profile = 'aiyu-ops'
        source.chat_id = source.thread_id = 'standalone-ops-task'
        source.parent_chat_id = 'ops-main'
        entry = store.get_or_create_session(source)
        assert entry.session_key.startswith('agent:main:')
        policy = value.service.policy['coordination']
        policy['task_parents']['aiyu-ops'] = ['ops-main']
        current, sid, selected = persisted_source(home, entry.session_key, policy)
        assert current.profile == 'aiyu-ops' and sid == entry.session_id and selected == home
    finally:
        store.close_all_db_handles()
