"""Cold work initialization through real Base intake, AIAgent and tool workers.

Only Discord I/O and provider responses are simulated; no human bootstrap event
or manually accepted internal receipt is used for the new work source.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.agent_coordination import submit_document
from gateway.coordination_receipts import DOCUMENT_TURN
from gateway.native_endpoint import NATIVE_TURN
from gateway.profile_routing import ProfileRoute
from hermes_cli.kanban_task_source import read_sources
from tests.gateway import test_strict_compression_return as native_fixtures
from tests.gateway.test_native_queued_turn_binding import _response

shared_native = native_fixtures.shared_native
live_model = native_fixtures.live_model
strict_intake = native_fixtures.strict_intake
pytestmark = [pytest.mark.platforms('posix'), pytest.mark.usefixtures('isolated_entrypoint_bootstrap')]


@pytest.mark.asyncio
async def test_cold_ops_receipt_enters_real_gateway_and_model_before_private_return(strict_intake, monkeypatch, record_testsuite_property):
    import model_tools
    import run_agent
    import tools.file_tools
    from gateway.run import GatewayRunner
    from tools.registry import registry

    test = strict_intake
    model, runner = test.model, test.model.runner
    service, adapter = model.service, test.adapter
    # The reused single-profile fixture pins a default DB. Restore the normal
    # production per-profile handle resolver for this multiplexed consumer test.
    runner._init_session_db()
    service.policy['execution_enabled'] = True
    service.policy['coordination']['task_parents']['aiyu-ops'] = ['ops-projects']
    runner.config.profile_routes.append(ProfileRoute('ops-work', 'discord', 'aiyu-ops', chat_id='ops-projects'))
    model.plan.fail, model.plan.transition, model.plan.after_tool = False, 'none', None
    events, file_results = [], []

    async def channel(tid):
        return SimpleNamespace(id=int(tid), parent_id='ops-projects', guild=SimpleNamespace(id='guild'),
            type=SimpleNamespace(name='public_thread'), archived=False, locked=False)

    adapter._resolve_channel = channel
    names = ('coordinate_session', 'read_file')
    schemas = [{'type': 'function', 'function': registry.get_entry(name).schema} for name in names]
    monkeypatch.setattr(model_tools, 'get_tool_definitions', lambda **kwargs: schemas)
    original_dispatch = model_tools.handle_function_call

    def dispatch(name, arguments, *args, **kwargs):
        result = original_dispatch(name, arguments, *args, **kwargs)
        if name == 'read_file':
            file_results.append((NATIVE_TURN.get(), result))
        return result

    monkeypatch.setattr(model_tools, 'handle_function_call', dispatch)

    def script(agent, **kwargs):
        model.plan.calls += 1
        step = model.plan.calls
        turn = DOCUMENT_TURN.get()
        if turn is None:
            return _response('no ordinary model invocation expected')
        work = turn.phase == 'request' and turn.key.endswith(':201')
        if work and step == 1:
            name, args = 'read_file', {'path': str(model.path)}
        elif step == 1 or (work and step == 2):
            name, args = 'coordinate_session', model.plan.args
        else:
            return _response('isolated full native result')
        return _response(tool_calls=[SimpleNamespace(id='bootstrap-call-' + str(step), type='function',
            function=SimpleNamespace(name=name, arguments=json.dumps(args)))])

    monkeypatch.setattr(run_agent.AIAgent, 'script', script)
    real_handle = GatewayRunner._handle_message_with_agent.__get__(runner)

    async def native_handle(event, source, key, generation):
        turn = DOCUMENT_TURN.get()
        assert turn is not None and event._gateway_accepted
        assert event.internal and event.allow_gateway_control is False
        events.append((source.chat_id, turn.phase, key, turn.work['request_id']))
        model.plan.keys.add(key)
        model.plan.calls = 0
        args = dict(action='complete', request_id=turn.work['request_id'], outcome='completed')
        if turn.phase == 'request' and source.chat_id == 'default-main':
            args = dict(request_id='native-peer-01',task_id='task-one',handoff_path=str(model.path),target='peer')
        elif turn.phase == 'request' and source.chat_id == 'ops-main':
            args = dict(action='initialize_task',request_id='native-work-01',work_task_id='task-one-ops',thread_id='201')
        model.plan.args = args
        return await real_handle(event, source, key, generation)

    monkeypatch.setattr(runner, '_handle_message_with_agent', native_handle)
    before = read_sources(service.home)['task-one']
    assert len(runner.session_store._entries) == 3
    await submit_document(service,model.entry.session_key,model.entry.session_id,service.home,
                          'native-root-01','task-one',str(model.path),'main')
    for _ in range(8):
        await service.tick()
        if service.journal.get('native-root-01')['status'] == 'delivered':
            break
    assert [chat for chat,_,_,_ in events] == ['default-main','ops-main','201','ops-main','default-main','shared-thread'], (
        events, service.journal.pending())
    assert all('error' not in item['result'] for item in model.observed), model.observed
    assert file_results and 'isolated native task' in file_results[0][1]
    assert file_results[0][0].home == service.home/'profiles/aiyu-ops'
    assert file_results[0][0].key == read_sources(service.home)['task-one-ops']['session_key']
    assert read_sources(service.home)['task-one'] == before
    work = read_sources(service.home)['task-one-ops']
    assert work['profile'] == 'aiyu-ops' and work['session_id'] != model.entry.session_id
    record = service.journal.get('native-work-01')
    assert record['parent_request_id'] == 'native-peer-01' and record['root_request_id'] == 'native-root-01'
    assert record['status'] == 'delivered' and record['outcome'] == 'completed'
    assert service.journal.get('native-root-01')['status'] == 'delivered'
    cold = [r for r in test.resolutions if r['key'] == work['session_key']]
    assert len(cold) == 1 and cold[0]['internal'] and cold[0]['strict'] and cold[0]['receipt'] is not None
    record_testsuite_property('native_cold_work_chain',json.dumps(dict(
        chats=[chat for chat,_,_,_ in events],cold_strict_receipts=len(cold),
        business_source_unchanged=True,ops_first_action='read_file',
        private_root_status=service.journal.get('native-root-01')['status'])))
    runner.close_all_session_db_handles()
    await asyncio.gather(*list(adapter._session_tasks.values()), return_exceptions=True)
