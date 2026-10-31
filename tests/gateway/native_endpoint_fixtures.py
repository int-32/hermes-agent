"""Isolated real SessionStore and GatewayRunner fixtures; only transport/provider are simulated."""
import asyncio
import json
import pytest_asyncio

@pytest_asyncio.fixture
async def shared_native(tmp_path, monkeypatch, isolated_entrypoint_bootstrap):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore, AsyncSessionStore
    from tests.gateway.test_multiplex_transport_matrix import _stub
    from gateway import agent_handoff as handoff, status
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, projects_db
    from hermes_cli.kanban_task_source import accept_task
    import tools.agent_handoff_tool
    home = tmp_path / 'home'; ops = home / 'profiles' / 'aiyu-ops'; ops.mkdir(parents=True)
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    monkeypatch.setattr('hermes_cli.profiles.profiles_to_serve', lambda **kw: [('default', home), ('aiyu-ops', ops)])
    monkeypatch.setattr('hermes_cli.profiles.get_profile_dir', lambda n: home if n == 'default' else ops)
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda n: True)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(sessions_dir=home / 'sessions', multiplex_profiles=True)
    runner.config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = parse_profile_routes([dict(name='ops', platform='discord', profile='aiyu-ops', chat_id='ops-main')])
    runner._primary_profile_name = 'default'
    adapter = _stub(Platform.DISCORD, runner, 'shared-discord')
    runner.adapters, runner._profile_adapters = {Platform.DISCORD: adapter}, {'aiyu-ops': {}}
    runner._is_user_authorized_for_source = lambda s, **kw: s.user_id in {'owner','colleague'}
    runner._running, runner._draining, runner._external_drain_active = True, False, False
    runner._gateway_loop = asyncio.get_running_loop()
    runner._served_profile_for_home = lambda h: 'default' if h == home else 'aiyu-ops'
    runner.session_store = SessionStore(home / 'sessions', runner.config)
    runner._async_session_store = AsyncSessionStore(runner.session_store)
    mains={}
    for profile, chat in [('default','default-main'),('aiyu-ops','ops-main')]:
        source=adapter.build_source(chat_id=chat, chat_type='group', user_id='owner', scope_id='guild')
        runner._canonicalize(source)
        entry=await runner.async_session_store.get_or_create_session(source)
        mains[profile]=dict(session_key=entry.session_key,session_id=entry.session_id,chat_id=chat)
    source=adapter.build_source(chat_id='shared-thread',chat_type='thread',thread_id='shared-thread',parent_chat_id='projects',user_id='owner',scope_id='guild')
    runner._canonicalize(source)
    entry=await runner.async_session_store.get_or_create_session(source)
    current=adapter.build_source(chat_id='shared-thread',chat_type='thread',thread_id='shared-thread',parent_chat_id='projects',user_id='colleague',scope_id='guild')
    runner._canonicalize(current)
    shared=await runner.async_session_store.get_or_create_session(current)
    assert shared.session_id==entry.session_id and shared.origin.user_id=='owner'
    path=tmp_path/'docs/project/task-one/HANDOFF.md';path.parent.mkdir(parents=True);path.write_text('isolated native task')
    policy=dict(enabled=True,execution_enabled=False,coordination=dict(enabled=True,owner_user_id='owner',guild_id='guild',handoff_root=str(tmp_path/'docs'),mains=mains,task_parents={'default':['projects']}))
    kb.create_board('registry',tracking_only=True)
    with projects_db.connect_closing() as conn:
        project=projects_db.create_project(conn,name='Project',slug='system',primary_path=str(home))
    with kbc.connect_closing(board='registry') as conn:
        accept_task(conn,policy=policy['coordination'],source=source,session_key=entry.session_key,session_id=entry.session_id,
                    profile_home=home,task_id='task-one',document_project='project',project_id=project,title='Offline native task')
    service=handoff.HandoffService(runner,home,policy)
    monkeypatch.setattr(handoff,'_service',service)
    monkeypatch.setattr(handoff,'is_engaged',lambda:False)
    monkeypatch.setattr(status,'_get_gateway_lock_path',lambda:home/'runtime/gateway.lock')
    assert status.acquire_gateway_runtime_lock()
    try:
        yield service,path,current,entry
    finally:
        handoff.clear_handoff_service(runner)
        status.release_gateway_runtime_lock()
        runner.session_store.close_all_db_handles()
