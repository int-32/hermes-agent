"""Real SessionStore producers with isolated stores and simulated native transport."""
import json
from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from tests.gateway.test_agent_coordination import make_star


def produced_star(tmp_path, monkeypatch, *, multiplex=True, implicit=False, custom_scope=False):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    home = tmp_path / '.hermes'; home.mkdir()
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    service, path = make_star(home, monkeypatch, native_tasks=False)
    ops = home / 'profiles' / 'aiyu-ops'; ops.mkdir(parents=True)
    (ops / 'config.yaml').write_text('{}', encoding='utf-8')
    monkeypatch.setattr('hermes_cli.profiles.get_profile_dir', lambda p: home if p == 'default' else ops)
    policy = service.policy['coordination']
    policy['task_parents'] = {'default': ['default-main'], 'aiyu-ops': ['ops-main']}
    policy['tasks'] = {}
    raw = {'agent_handoff': service.policy, 'sessions_dir': str(home / ('custom-sessions' if custom_scope else 'sessions'))}
    if not implicit:
        raw['gateway'] = {'multiplex_profiles': multiplex}
    (home / 'config.yaml').write_text(json.dumps(raw), encoding='utf-8')
    config = GatewayConfig.from_dict(raw)
    config.write_sessions_json = False
    if implicit:
        from hermes_cli.gateway_multiplex_mode import resolve_multiplex_mode
        monkeypatch.setattr('hermes_cli.gateway_multiplex_mode.implicit_multiplex_blocker', lambda: None)
        monkeypatch.setattr('hermes_cli.gateway_multiplex_mode.standalone_launcher_decision', lambda _: None)
        assert resolve_multiplex_mode(config).enabled
    store = SessionStore(config.sessions_dir, config)
    entries = {}
    for chat, profile, parent in [('default-main', 'default', None), ('default-task', 'default', 'default-main'),
                                   ('ops-main', 'aiyu-ops', None), ('ops-task', 'aiyu-ops', 'ops-main')]:
        if profile != 'default' and not config.multiplex_profiles:
            continue
        source = SessionSource(Platform.DISCORD, chat, chat_type='thread' if parent else 'group',
            user_id='owner', scope_id='guild', profile=profile, thread_id=chat if parent else None,
            parent_chat_id=parent)
        entries[chat] = store.get_or_create_session(source)
    runner = service.runner
    runner.entries = entries
    runner.async_session_store = AsyncSessionStore(store)
    runner._session_key_for_source = store._generate_session_key
    runner._resolve_profile_home_for_source = lambda s: home if (s.profile or 'default') == 'default' else ops

    def canonical(source):
        source.profile = entries[source.chat_id].origin.profile
        return SimpleNamespace(runtime_profile=source.profile or 'default')

    runner._canonicalize = canonical
    async def handle(event):
        from gateway.coordination_receipts import DOCUMENT_TURN, complete_turn
        from tests.gateway.test_agent_handoff import Runner
        turn = DOCUMENT_TURN.get()
        if turn:
            await complete_turn(service, runner._session_key_for_source(event.source),
                entries[event.source.chat_id].session_id, runner._resolve_profile_home_for_source(event.source),
                turn.work['request_id'], 'completed')
        await Runner.handle(runner, event)
    runner.adapter.handle_message = handle
    for profile, chat in [('default', 'default-main'), ('aiyu-ops', 'ops-main')]:
        if chat in entries:
            entry = entries[chat]
            policy['mains'][profile] = dict(session_key=entry.session_key, session_id=entry.session_id, chat_id=chat)
    # Persist the permission root after the real producer selected its actual keys.
    (home / 'config.yaml').write_text(json.dumps(raw), encoding='utf-8')
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_task_source import accept_task
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    kb.create_board('registry', tracking_only=True)
    with kbc.connect_closing(board='registry') as conn:
        for task, chat in [('task-one', 'default-task'), ('ops-one', 'ops-task')]:
            if chat not in entries:
                continue
            entry = entries[chat]
            accept_task(conn, policy=policy, source=entry.origin, session_key=entry.session_key,
                session_id=entry.session_id, profile_home=runner._resolve_profile_home_for_source(entry.origin),
                task_id=task, document_project='project', project_id=service.fixture_project_id, title=task)
    return SimpleNamespace(service=service, path=path, home=home, store=store, entries=entries, config=config)
