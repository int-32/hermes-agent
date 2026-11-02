"""Trusted receiving main initializes its own work lane without a human input or source transfer."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.agent_coordination import submit_document
from gateway.agent_handoff import HandoffService
from gateway.config import GatewayConfig, Platform
from gateway.internal_receipt import capture_internal_response, finish_internal_receipt
from gateway.platforms.event import ProcessingOutcome
from gateway.profile_routing import ProfileRoute
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.session_context import native_model_turn_scope
from gateway.session_identity import canonical_identity
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, projects_db
from hermes_cli.kanban_task_source import accept_task, read_sources
from tests.gateway.test_agent_handoff import FakeAdapter, native_owner

pytestmark = pytest.mark.platforms('posix')


class NativeRunner:
    def __init__(self, home):
        from gateway.run import GatewayRunner
        self.home = home
        self._running, self._draining, self._external_drain_active = True, False, False
        self._primary_profile_name = 'default'
        self.config = GatewayConfig(sessions_dir=home/'sessions', multiplex_profiles=True,
            write_sessions_json=False, profile_routes=[
                ProfileRoute('ops-main', 'discord', 'aiyu-ops', chat_id='101'),
                ProfileRoute('ops-work', 'discord', 'aiyu-ops', chat_id='400')])
        self.config._runtime_profile_homes = [('default', home), ('aiyu-ops', home/'profiles/aiyu-ops')]
        self.session_store = SessionStore(self.config.sessions_dir, self.config)
        self.async_session_store = AsyncSessionStore(self.session_store)
        self.received, self.actions, self.tool_replies = [], [], []
        self.allowed = True
        self.before_initialize = None
        self.initialization_changes = {}
        self.initialization_repeats = 1
        self.after_initialize = None
        self.ancestor_refusal = None
        self.own_main_refusal = None
        self.adapter = FakeAdapter(handle_message=self.handle, _resolve_channel=self.channel,
                                   _active_sessions={}, _pending_messages={})
        self.adapters = {Platform.DISCORD: self.adapter}
        self._profile_adapters = {}
        self._profile_name_for_source = GatewayRunner._profile_name_for_source.__get__(self)
        self._resolve_profile_home_for_source = GatewayRunner._resolve_profile_home_for_source.__get__(self)

    async def channel(self, thread_id):
        return SimpleNamespace(id=int(thread_id), parent_id=400, guild=SimpleNamespace(id=99),
                               type=SimpleNamespace(name='public_thread'), archived=False, locked=False)

    def _served_profile_for_home(self, home):
        return next((p for p, h in self.config._runtime_profile_homes if Path(home) == h), None)

    def _owning_profile(self, adapter, platform):
        return (adapter, None) if adapter is self.adapter else (None, None)

    def _transport_owner(self, source):
        return self.adapter, None

    def _canonicalize(self, source):
        return canonical_identity(source, runner=self, adapter=self.adapter, primary_home=self.home)

    def _session_key_for_source(self, source):
        return self.session_store._generate_session_key(source)

    def _restored_source(self, entry):
        return entry.origin

    def _is_user_authorized_for_source(self, source, **kwargs):
        return self.allowed and source.user_id == '42'

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _is_session_running(self, key):
        return False

    def _queue_depth(self, key, **kwargs):
        return 0

    def _profile_scope_for_source(self, source):
        from gateway.run import _profile_runtime_scope
        return _profile_runtime_scope(self._resolve_profile_home_for_source(source))

    @asynccontextmanager
    async def _plugin_injection_scope(self, source):
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
            yield

    async def handle(self, event):
        from gateway.native_job import current_document_turn
        from gateway.coordination_receipts import complete_turn
        from tools.registry import registry
        import tools.agent_handoff_tool
        import tools.file_tools
        self.received.append(event)
        event._gateway_accepted = True
        key = self._session_key_for_source(event.source)
        sid = self.session_store.lookup_by_session_key(key).session_id
        receipt = event._internal_turn_receipt
        with native_model_turn_scope(self, event.source, key, sid, job=receipt.job):
            turn = current_document_turn()
            args = None
            if turn.phase == 'request' and event.source.chat_id == '100':
                self.own_main_refusal = json.loads(await asyncio.to_thread(registry.dispatch, 'coordinate_session',
                    dict(action='initialize_task', request_id='not-peer-parent-01', work_task_id='forbidden-work',thread_id='202'),session_id=sid))
                args = dict(request_id='business-peer-01', task_id='business', handoff_path=str(self.document), target='peer')
            elif turn.phase == 'request' and event.source.chat_id == '101':
                if self.before_initialize is not None:
                    await self.before_initialize(self, event)
                args = dict(action='initialize_task', request_id='ops-work-request-01', work_task_id='business-ops', thread_id='201')
                args.update(self.initialization_changes)
            if args is not None:
                for _ in range(self.initialization_repeats if args.get('action') == 'initialize_task' else 1):
                    result = json.loads(await asyncio.to_thread(registry.dispatch, 'coordinate_session', args, session_id=sid))
                    self.tool_replies.append(result)
                if args.get("action") == "initialize_task" and self.after_initialize is not None:
                    await self.after_initialize(self, event)
            else:
                if event.source.chat_id == '201':
                    self.ancestor_refusal = json.loads(await asyncio.to_thread(registry.dispatch,'coordinate_session',
                        dict(request_id='actual-ancestor-loop',task_id='business-ops',
                             handoff_path=str(self.document.parents[1]/'business-ops/HANDOFF.md'),target='main'),session_id=sid))
                    result = registry.dispatch('read_file', {'path': str(self.document)}, session_id=sid)
                    self.actions.append({'profile':event.source.profile,'key':key,'sid':sid,'read_file':result})
                await complete_turn(self.service, key, sid, self._resolve_profile_home_for_source(event.source),
                                    turn.work['request_id'], 'blocked' if turn.child and turn.child.get('outcome') != 'completed' else 'completed')
        capture_internal_response(event, 'isolated private native result', successful=True)
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    home = tmp_path/'.hermes';home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home));monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home/'state.db')
    peer = home/'profiles/aiyu-ops';peer.mkdir(parents=True);(peer/'config.yaml').write_text('{}')
    from gateway import status
    assert status.acquire_gateway_runtime_lock()
    runner = NativeRunner(home)
    sources, entries = {}, {}
    for name, chat, parent in [('main','100',None),('ops','101',None),('business','200','300')]:
        source = SessionSource(Platform.DISCORD,chat,chat_type='thread' if parent else 'group',
            user_id='42',scope_id='99',parent_chat_id=parent,thread_id=chat if parent else None)
        runner._canonicalize(source)
        entry = runner.session_store.get_or_create_session(source)
        sources[name], entries[name] = source, entry
    kb.create_board('registry',tracking_only=True)
    with projects_db.connect_closing() as conn:
        project = projects_db.create_project(conn,name='Fixture',slug='fixture',primary_path=str(home))
    doc = home/'documents/project/business/HANDOFF.md';doc.parent.mkdir(parents=True);doc.write_text('isolated business evidence nonce')
    policy = dict(enabled=True,execution_enabled=True,coordination=dict(enabled=True,owner_user_id='42',guild_id='99',
        handoff_root=str(home/'documents'),mains={p:dict(session_key=entries[n].session_key,session_id=entries[n].session_id,chat_id=sources[n].chat_id)
            for p,n in [('default','main'),('aiyu-ops','ops')]},task_parents={'default':['300'],'aiyu-ops':['400']}))
    service = HandoffService(runner,home,policy);runner.service=service;runner.document=doc
    monkeypatch.setattr('gateway.agent_handoff._service',service);monkeypatch.setattr('gateway.agent_handoff.is_engaged',lambda:False)
    runner._gateway_loop = None  # async tests bind the running loop before dispatch
    with kbc.connect_closing(board='registry') as conn:
        accept_task(conn,policy=policy['coordination'],source=sources['business'],session_key=entries['business'].session_key,
            session_id=entries['business'].session_id,profile_home=home,task_id='business',document_project='project',project_id=project,title='Business')
    yield service,doc,sources,entries
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_real_store_cold_ops_lane_receives_private_work_and_returns_through_both_mains(bootstrap, record_testsuite_property):
    service, doc, _sources, entries = bootstrap
    runner = service.runner;runner._gateway_loop=asyncio.get_running_loop()
    import gateway.agent_handoff as h
    assert h._service is service
    assert h.completion_owner(service)
    assert not h.is_engaged()
    before=read_sources(service.home)['business']
    await submit_document(service,entries['business'].session_key,entries['business'].session_id,service.home,
                          'business-root-01','business',str(doc),'main')
    for _ in range(8):
        await service.tick()
        if service.journal.get('business-root-01')['status']=='delivered':break
    assert all('error' not in r for r in runner.tool_replies), runner.tool_replies
    assert runner.actions, 'independent Ops has not executed its first real file handler'
    assert runner.actions[0]['profile']=='aiyu-ops'
    assert 'private receiving peer-main' in runner.own_main_refusal['error']
    assert 'recursive' in runner.ancestor_refusal['error']
    assert service.journal.get('actual-ancestor-loop') is None
    assert 'isolated business evidence nonce' in runner.actions[0]['read_file']
    assert read_sources(service.home)['business']==before
    work=read_sources(service.home)['business-ops']
    assert work['profile']=='aiyu-ops' and work['native_id']!=before['native_id']
    assert work['work_for']['task_id']=='business'
    assert service.journal.get('ops-work-request-01')['parent_request_id']=='business-peer-01'
    assert service.journal.get('ops-work-request-01')['root_request_id']=='business-root-01'
    assert service.journal.get('business-root-01')['status']=='delivered'
    assert [e.source.chat_id for e in runner.received]==['100','101','201','101','100','200']
    with kbc.connect_closing(board='registry') as conn:
        assert kb.board_tracking_only(conn)
        assert kb.get_task(conn,work["native_id"]).assignee=="aiyu-ops"
        observations=[e for e in kb.list_events(conn,work["native_id"]) if e.kind=="native_task_observed"]
        assert observations, "independent execution progress must belong to its own work card"
        assert all(e.payload["event"]["task_id"]=="business-ops" for e in observations)
        assert all(kb.list_runs(conn,s['native_id'])==[] for s in read_sources(service.home).values())
    record_testsuite_property('native_ops_chain',json.dumps({'profiles':[e.source.profile or 'default' for e in runner.received],
        'business_unchanged':True,'root':'business-root-01','work':'ops-work-request-01','first_action':'real read_file','tracking_only':True,'runs':0}))
