"""Native turn identity survives cached agents, tool bridges and thread hops.

No model, Discord adapter, live gateway or operator journal is invoked. All stores are disposable; queued in these tests never means live delivery.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

from tests.gateway.test_agent_handoff import native_owner

import pytest

from tests.gateway.test_agent_coordination import make_star
from gateway.session_context import native_model_turn_scope


@pytest.fixture
def native_call(tmp_path, monkeypatch, isolated_entrypoint_bootstrap):
    from gateway.run import GatewayRunner
    import gateway.agent_handoff as handoff
    import model_tools
    import tools.agent_handoff_tool
    from tools.registry import registry

    service, path = make_star(tmp_path, monkeypatch)
    service.runner._served_profile_for_home = lambda home: 'default'
    monkeypatch.setattr(handoff, '_service', service)
    monkeypatch.setattr('hermes_constants.get_hermes_home', lambda: tmp_path / 'default')
    monkeypatch.setenv('HERMES_SESSION_ID', '')  # restore the constructor's legacy env write on teardown
    definition = {'type': 'function', 'function': registry.get_entry('coordinate_session').schema}
    monkeypatch.setattr(model_tools, 'get_tool_definitions', lambda **kw: [definition])
    entry = service.runner.entries['default-main']
    context = SimpleNamespace(source=entry.origin, session_key='default-main', session_id=entry.session_id)
    runner = SimpleNamespace(adapters={})
    args = {'request_id': 'offline-dynamic-0001', 'task_id': 'task-one',
            'handoff_path': str(path), 'target': 'peer'}
    return GatewayRunner, runner, context, service, args


@pytest.mark.asyncio
@pytest.mark.parametrize('fresh', [False, True], ids=['cached', 'fresh-constructor'])
@pytest.mark.parametrize('mode', ['direct', 'tool-call-bridge', 'executor-unwrap'])
async def test_cached_and_fresh_turns_keep_native_identity_through_dynamic_dispatch(native_call, monkeypatch, fresh, mode):
    import gateway.agent_handoff as handoff
    from gateway.session_context import _SESSION_ID, _SESSION_KEY, _UNSET, scoped_current_session_id
    from tools.thread_context import propagate_context_to_thread
    import model_tools

    gateway, runner, context, service, args = native_call
    service.runner._gateway_loop = asyncio.get_running_loop()
    original_bound_request = handoff._bound_request
    observations = []

    def observe(session_id, **kw):
        observations.append({'service_present': handoff._service is service,
            'coordination_available': handoff.handoff_available(coordination=True),
            'session_key_set': _SESSION_KEY.get() is not _UNSET,
            'session_key': _SESSION_KEY.get(), 'context_session_id': _SESSION_ID.get(),
            'handler_session_id': session_id, 'id_equal': _SESSION_ID.get() == session_id})
        return original_bound_request(session_id, **kw)

    monkeypatch.setattr(handoff, '_bound_request', observe)

    def dispatch():
        name, arguments = ('coordinate_session', args) if mode == 'direct' else (
            'tool_call', {'calls': [{'name': 'coordinate_session', 'arguments': args}]})
        if mode == 'executor-unwrap':
            from agent.tool_executor import _unwrap_tool_search_call, _resolve_sequential_dispatch, _ToolCallRef
            agent = SimpleNamespace(session_id=context.session_id, enabled_toolsets=['agent_handoff'],
                disabled_toolsets=None, valid_tool_names={'coordinate_session'}, quiet_mode=False,
                _context_engine_tool_names=set(), _memory_manager=None)
            name, arguments, blocked = _unwrap_tool_search_call(agent, name, arguments)
            assert name == 'coordinate_session' and blocked is None
            ref = _ToolCallRef(name, arguments, context.session_id, 'offline-call', [])
            return _resolve_sequential_dispatch(agent, ref, []).execute(arguments)
        return model_tools.handle_function_call(name, arguments, session_id=context.session_id,
            enabled_toolsets=['agent_handoff'], skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True, skip_tool_execution_middleware=True)

    def turn_worker():
        with native_model_turn_scope(service.runner, context.source, context.session_key, context.session_id):
            # Only fresh construction publishes the id in production; cached turns omit this.
            if fresh:
                from agent.agent_init import _publish_session_id
                _publish_session_id(context.session_id)
            with ThreadPoolExecutor(max_workers=1) as tool_pool:
                return tool_pool.submit(propagate_context_to_thread(dispatch)).result(timeout=5)

    with scoped_current_session_id():
        tokens = gateway._set_session_env(runner, context)
        try:
            with ThreadPoolExecutor(max_workers=1) as turn_pool:
                runner._get_executor = lambda: turn_pool
                response = json.loads(await gateway._run_in_executor_with_context(runner, turn_worker))
        finally:
            gateway._clear_session_env(runner, tokens)

    assert len(observations) == 1
    seen = observations[0]
    assert seen['service_present'] and seen['coordination_available'] and seen['session_key_set']
    assert seen['session_key'] == context.session_key
    assert seen['handler_session_id'] == context.session_id
    assert seen['id_equal'] is True
    assert seen['context_session_id'] == context.session_id
    assert response['status'] == 'queued'
    assert service.journal.get(args['request_id'])['status'] == 'queued'
    assert not service.runner.received and not service.runner.sent


def test_gateway_must_bind_native_session_id_on_each_cached_turn(native_call):
    """Each native turn binds its current id without relying on agent construction."""
    from gateway.session_context import _SESSION_ID, scoped_current_session_id

    gateway, runner, context, _, _ = native_call
    with scoped_current_session_id():
        tokens = gateway._set_session_env(runner, context)
        try:
            assert _SESSION_ID.get() == context.session_id
        finally:
            gateway._clear_session_env(runner, tokens)


@pytest.mark.asyncio
async def test_concurrent_profiles_keep_ids_isolated_across_native_executor(native_call):
    from threading import Barrier
    from gateway.session_context import _SESSION_ID, _SESSION_KEY, _SESSION_PROFILE

    gateway, runner, _, service, _ = native_call
    barrier = Barrier(2)

    async def turn(key):
        entry = service.runner.entries[key]
        context = SimpleNamespace(source=entry.origin, session_key=key, session_id=entry.session_id)
        tokens = gateway._set_session_env(runner, context)
        try:
            def read_twice():
                before = (_SESSION_KEY.get(), _SESSION_ID.get(), _SESSION_PROFILE.get())
                barrier.wait(timeout=5)
                after = (_SESSION_KEY.get(), _SESSION_ID.get(), _SESSION_PROFILE.get())
                return before, after
            result = await gateway._run_in_executor_with_context(runner, read_twice)
            expected = (key, entry.session_id, entry.origin.profile)
            assert result == (expected, expected)
            assert _SESSION_ID.get() == entry.session_id
        finally:
            gateway._clear_session_env(runner, tokens)
        assert _SESSION_KEY.get() == _SESSION_ID.get() == ''

    with ThreadPoolExecutor(max_workers=2) as pool:
        runner._get_executor = lambda: pool
        await asyncio.wait_for(asyncio.gather(turn('default-main'), turn('ops-main')), timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True], ids=['normal', 'exception'])
async def test_cleared_native_turn_cannot_reuse_identity_from_environment(native_call, failed, monkeypatch):
    from gateway.session_context import _SESSION_ID, _SESSION_KEY, scoped_current_session_id
    from tools.registry import registry

    gateway, runner, context, service, args = native_call
    service.runner._gateway_loop = asyncio.get_running_loop()
    monkeypatch.setenv('HERMES_SESSION_ID', context.session_id)
    monkeypatch.setenv('HERMES_SESSION_KEY', context.session_key)
    with scoped_current_session_id():
        tokens = gateway._set_session_env(runner, context)
        try:
            try:
                assert _SESSION_ID.get() == context.session_id
                if failed:
                    raise RuntimeError('turn failed')
            finally:
                gateway._clear_session_env(runner, tokens)
        except RuntimeError as error:
            assert failed and str(error) == 'turn failed'
        assert _SESSION_ID.get() == _SESSION_KEY.get() == ''
        response = json.loads(await asyncio.to_thread(registry.dispatch, 'coordinate_session', args,
                                                      session_id=context.session_id))
        assert response == {'error': 'requires a bound native gateway model turn'}
    assert service.journal.get(args['request_id']) is None


@pytest.mark.asyncio
async def test_compression_rebinds_worker_and_next_cached_turn_but_reset_stays_refused(native_call):
    from agent.conversation_compression import _rebind_session_context
    from gateway.session_context import _SESSION_ID, scoped_current_session_id
    from hermes_state import SessionDB
    from tools.registry import registry

    gateway, runner, context, service, args = native_call
    service.runner._gateway_loop = asyncio.get_running_loop()
    old = context.session_id
    tip = old + '-compressed'
    home = service.runner.home / 'default'
    home.mkdir(exist_ok=True)
    db = SessionDB(home / 'state.db')
    try:
        db.create_session(old, source='discord')
        db.end_session(old, 'compression')
        db.create_session(tip, source='discord', parent_session_id=old)
        service.runner.entries[context.session_key].session_id = tip

        def compressed_worker():
            with native_model_turn_scope(service.runner, context.source, context.session_key, old):
                _rebind_session_context(tip)
                assert _SESSION_ID.get() == tip
                return registry.dispatch('coordinate_session', args, session_id=tip)

        with scoped_current_session_id():
            tokens = gateway._set_session_env(runner, context)
            try:
                response = json.loads(await asyncio.to_thread(compressed_worker))
                assert response['status'] == 'queued'
                assert _SESSION_ID.get() == old  # worker rotation does not clobber the parent task
            finally:
                gateway._clear_session_env(runner, tokens)

            context.session_id = tip
            tokens = gateway._set_session_env(runner, context)
            try:
                assert _SESSION_ID.get() == tip
                stale = json.loads(await asyncio.to_thread(registry.dispatch, 'coordinate_session',
                    {**args, 'request_id': 'stale-worker-0001'}, session_id=old))
                assert stale == {'error': 'requires a bound native gateway model turn'}
                with native_model_turn_scope(service.runner, context.source, context.session_key, tip):
                    valid = json.loads(await asyncio.to_thread(registry.dispatch, 'coordinate_session',
                        {**args, 'request_id': 'next-cached-0001'}, session_id=tip))
                assert valid['status'] == 'queued'
            finally:
                gateway._clear_session_env(runner, tokens)

            db.end_session(tip, 'session_reset')
            reset = tip + '-reset'
            db.create_session(reset, source='discord', parent_session_id=tip)
            context.session_id = reset
            service.runner.entries[context.session_key].session_id = reset
            tokens = gateway._set_session_env(runner, context)
            try:
                assert _SESSION_ID.get() == reset
                with native_model_turn_scope(service.runner, context.source, context.session_key, reset):
                    refused = json.loads(await asyncio.to_thread(registry.dispatch, 'coordinate_session',
                        {**args, 'request_id': 'reset-refusal-0001'}, session_id=reset))
                assert refused == {'error': 'registered session reset or forked'}
            finally:
                gateway._clear_session_env(runner, tokens)
        assert service.journal.get('stale-worker-0001') is None
        assert service.journal.get('reset-refusal-0001') is None
        assert not service.runner.received and not service.runner.sent
    finally:
        db.close()
