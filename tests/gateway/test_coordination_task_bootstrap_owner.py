"""Independent consumer counterexample; only fixture transport await is instrumented."""
import asyncio
import json
import pytest
from tests.gateway import test_coordination_task_bootstrap as fixtures
from tests.gateway.test_coordination_task_bootstrap_guards import reach_ops
bootstrap = fixtures.bootstrap
from tests.gateway.test_agent_handoff import native_owner
pytestmark=pytest.mark.platforms('posix')

@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['clear','release','teardown'])
async def test_reuse_cannot_enqueue_fresh_child_after_actual_owner_loss_during_resolver(bootstrap,record_testsuite_property,operation):
    from tools.registry import registry
    from gateway import agent_handoff as handoff, status
    service,_,_,_=bootstrap
    runner=service.runner
    observed=[]
    async def after(runner,event):
        original=runner.adapter._resolve_channel
        async def channel(tid):
            result=await original(tid)
            if operation=='clear':handoff.clear_handoff_service(runner)
            elif operation=='release':status.release_gateway_runtime_lock()
            else:
                from types import MethodType,SimpleNamespace
                from unittest.mock import AsyncMock
                from gateway.run_plugin_injection import GatewayPluginInjectionMixin
                from tests.gateway.test_coordination_settlement_owner import lifecycle
                runner._restart_requested=False
                runner._clear_plugin_message_injector=MethodType(GatewayPluginInjectionMixin._clear_plugin_message_injector,runner)
                runner._mark_api_runs_shutdown_requested=lambda:None
                runner._cancel_secondary_profile_reconnect_tasks=AsyncMock()
                runner._notify_active_sessions_of_shutdown=AsyncMock()
                runner._active_work_count=lambda:1
                runner._update_runtime_status=lambda *a:None
                await lifecycle(SimpleNamespace(service=service),'teardown')
            return result
        runner.adapter._resolve_channel=channel
        sid=runner.session_store.lookup_by_session_key(runner._session_key_for_source(event.source)).session_id
        result=json.loads(await asyncio.to_thread(registry.dispatch,'coordinate_session',dict(
            action='initialize_task',request_id='independent-after-owner-loss',work_task_id='business-ops',thread_id='201'),session_id=sid))
        record=service.journal.get('independent-after-owner-loss')
        observed.append(dict(operation=operation,result=result,record=record,active_service=handoff._service is service,owns_runtime=status.owns_gateway_runtime_lock(),running=runner._running))
        runner.adapter._resolve_channel=original
        # Restore only the isolated lifecycle for orderly old-request teardown.
        handoff._service=service
        runner._running=True
        runner._draining=False
        if not status.owns_gateway_runtime_lock():assert status.acquire_gateway_runtime_lock()
    runner.after_initialize=after
    await reach_ops(bootstrap)
    record_testsuite_property('owner_loss_counterexample',json.dumps(observed))
    assert len(observed)==1
    assert 'error' in observed[0]['result'] and observed[0]['record'] is None, observed

@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['clear','release'])
async def test_cold_allocation_does_refuse_the_same_owner_loss(bootstrap,operation,record_testsuite_property):
    from gateway import agent_handoff as handoff,status
    from hermes_cli.kanban_task_source import read_sources
    service,_,_,_=bootstrap
    runner=service.runner
    original=runner.adapter._resolve_channel
    async def channel(tid):
        result=await original(tid)
        if operation=='clear':handoff.clear_handoff_service(runner)
        else:status.release_gateway_runtime_lock()
        return result
    async def before(*args):runner.adapter._resolve_channel=channel
    async def after(*args):
        runner.adapter._resolve_channel=original
        handoff._service=service
        if not status.owns_gateway_runtime_lock():assert status.acquire_gateway_runtime_lock()
    runner.before_initialize=before
    runner.after_initialize=after
    result=await reach_ops(bootstrap)
    record_testsuite_property('cold_owner_loss_control',json.dumps(dict(operation=operation,result=result)))
    assert 'obsolete native owner' in result['error']
    assert service.journal.get('ops-work-request-01') is None
    assert set(read_sources(service.home))=={'business'}
    assert len(runner.session_store._entries)==3 and not runner.actions


@pytest.mark.asyncio
@pytest.mark.parametrize('reuse', [False, True], ids=['cold','reuse'])
async def test_release_reacquire_during_resolver_cannot_reuse_boolean_ownership(bootstrap,reuse):
    from gateway import status
    from tools.registry import registry
    service,_,_,_=bootstrap;runner=service.runner;observed=[]
    async def invoke(runner,event):
        original=runner.adapter._resolve_channel
        async def channel(tid):
            result=await original(tid)
            status.release_gateway_runtime_lock()
            assert status.acquire_gateway_runtime_lock()
            return result
        runner.adapter._resolve_channel=channel
        if reuse:
            sid=runner.session_store.lookup_by_session_key(runner._session_key_for_source(event.source)).session_id
            observed.append(json.loads(await asyncio.to_thread(registry.dispatch,'coordinate_session',dict(
                action='initialize_task',request_id='aba-new-work-child',work_task_id='business-ops',thread_id='201'),session_id=sid)))
            runner.adapter._resolve_channel=original
    if reuse:runner.after_initialize=invoke
    else:runner.before_initialize=invoke
    result=await reach_ops(bootstrap)
    result=observed[0] if reuse else result
    assert status.owns_gateway_runtime_lock()
    assert 'runtime owner changed' in result['error']
    assert service.journal.get('aba-new-work-child' if reuse else 'ops-work-request-01') is None
    if not reuse:
        from hermes_cli.kanban_task_source import read_sources
        assert set(read_sources(service.home))=={'business'} and len(runner.session_store._entries)==3


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['clear','release','release-reacquire'])
async def test_owner_loss_at_real_insert_rolls_back_new_child(bootstrap,monkeypatch,operation):
    from contextlib import contextmanager
    from gateway import agent_handoff as handoff,status
    from tools.registry import registry
    service,_,_,_=bootstrap;runner=service.runner;observed=[]
    async def after(runner,event):
        original=service.journal.connect
        @contextmanager
        def at_insert():
            with original() as db:
                class Connection:
                    def execute(self,sql,*args):
                        result=db.execute(sql,*args)
                        if sql.startswith('INSERT INTO requests'):
                            actions={'clear':lambda:handoff.clear_handoff_service(runner),
                                     'release':status.release_gateway_runtime_lock,
                                     'release-reacquire':lambda: (status.release_gateway_runtime_lock(),status.acquire_gateway_runtime_lock())}
                            actions[operation]()
                        return result
                yield Connection()
        sid=runner.session_store.lookup_by_session_key(runner._session_key_for_source(event.source)).session_id
        with monkeypatch.context() as patch:
            patch.setattr(service.journal,'connect',at_insert)
            result=json.loads(await asyncio.to_thread(registry.dispatch,'coordinate_session',dict(
                action='initialize_task',request_id='insert-owner-loss',work_task_id='business-ops',thread_id='201'),session_id=sid))
        observed.append(result)
        handoff._service=service
        if not status.owns_gateway_runtime_lock():assert status.acquire_gateway_runtime_lock()
    runner.after_initialize=after
    await reach_ops(bootstrap)
    assert 'error' in observed[0],observed
    assert service.journal.get('insert-owner-loss') is None
    assert len(service.journal.children('business-peer-01'))==1


@pytest.mark.asyncio
async def test_committed_new_child_wins_before_actual_teardown(bootstrap,monkeypatch,record_testsuite_property):
    import threading
    from contextlib import contextmanager
    from types import MethodType,SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway import agent_handoff as handoff,status
    from gateway.run_plugin_injection import GatewayPluginInjectionMixin
    from tests.gateway.test_coordination_settlement_owner import lifecycle
    from tools.registry import registry
    service,_,_,_=bootstrap;runner=service.runner;observations=[];returned=[];errors=[]
    committed,contending,release=threading.Event(),threading.Event(),threading.Event()
    loop_thread=threading.current_thread()
    original_lock=handoff._HANDOFF_OWNER_LOCK
    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is loop_thread and committed.is_set() and not contending.is_set():
                acquired=original_lock.acquire(blocking=False)
                observations.append(dict(critical_section_protected=not acquired,running=runner._running))
                contending.set()
                if not acquired:original_lock.acquire()
            else:original_lock.acquire()
            return self
        def __exit__(self,*args):original_lock.release()
    async def after(runner,event):
        original=service.journal.connect
        @contextmanager
        def after_commit():
            inserted=False
            with original() as db:
                class Connection:
                    def execute(self,sql,*args):
                        nonlocal inserted
                        result=db.execute(sql,*args)
                        inserted |= sql.startswith('INSERT INTO requests')
                        return result
                yield Connection()
            if inserted:
                committed.set()
                assert release.wait(10),'observer must release committed writer'
        def observe():
            try:
                assert contending.wait(10),'teardown must contend on the real owner lock'
                with original() as db:
                    row=db.execute('SELECT record FROM requests WHERE id=?',('committed-work-child',)).fetchone()
                observations.append(dict(durable_child=json.loads(row[0]),running=runner._running))
            except BaseException as error:errors.append(repr(error))
            finally:release.set()
        runner._restart_requested=False
        runner._clear_plugin_message_injector=MethodType(GatewayPluginInjectionMixin._clear_plugin_message_injector,runner)
        runner._mark_api_runs_shutdown_requested=lambda:None
        runner._cancel_secondary_profile_reconnect_tasks=AsyncMock()
        runner._notify_active_sessions_of_shutdown=AsyncMock()
        runner._active_work_count=lambda:1
        runner._update_runtime_status=lambda *a:None
        sid=runner.session_store.lookup_by_session_key(runner._session_key_for_source(event.source)).session_id
        observer=threading.Thread(target=observe)
        with monkeypatch.context() as patch:
            patch.setattr(handoff,'_HANDOFF_OWNER_LOCK',ObservedLock())
            patch.setattr(service.journal,'connect',after_commit)
            request=asyncio.create_task(asyncio.to_thread(registry.dispatch,'coordinate_session',dict(
                action='initialize_task',request_id='committed-work-child',work_task_id='business-ops',thread_id='201'),session_id=sid))
            try:
                assert await asyncio.to_thread(committed.wait,10),'new work must COMMIT'
                observer.start()
                await lifecycle(SimpleNamespace(service=service),'teardown')
                returned.append(json.loads(await request))
            finally:
                release.set()
                if observer.ident is not None:
                    await asyncio.to_thread(observer.join,10)
                    assert not observer.is_alive()
                await asyncio.gather(request,return_exceptions=True)
        # Only cleanup of this isolated test owner, after the commit/teardown outcome.
        handoff._service=service;runner._running=True;runner._draining=False
    runner.after_initialize=after
    await reach_ops(bootstrap)
    assert not errors,errors
    assert observations[0]['critical_section_protected'] and observations[0]['running']
    assert observations[1]['durable_child']['status']=='queued' and observations[1]['running']
    assert returned[0]['initialized'] and returned[0]['status']=='queued'
    assert service.journal.get('committed-work-child')['status']=='queued'
    record_testsuite_property('writer_wins_real_commit_teardown',json.dumps(dict(observations=observations,returned=returned)))


@pytest.mark.asyncio
@pytest.mark.parametrize('reuse',[False,True],ids=['cold','reuse'])
async def test_owner_token_precedes_the_actual_native_verification_await(bootstrap,monkeypatch,reuse):
    from gateway import status,native_endpoint
    from tools.registry import registry
    service,_,_,_=bootstrap;runner=service.runner;observed=[]
    original=native_endpoint.verify_current_source
    async def verify(*args):
        await original(*args)
        status.release_gateway_runtime_lock()
        assert status.acquire_gateway_runtime_lock()
    async def callback(runner,event):
        if not reuse:
            monkeypatch.setattr(native_endpoint,'verify_current_source',verify)
            return
        sid=runner.session_store.lookup_by_session_key(runner._session_key_for_source(event.source)).session_id
        with monkeypatch.context() as patch:
            patch.setattr(native_endpoint,'verify_current_source',verify)
            observed.append(json.loads(await asyncio.to_thread(registry.dispatch,'coordinate_session',dict(
                action='initialize_task',request_id='native-verify-owner-loss',work_task_id='business-ops',thread_id='201'),session_id=sid)))
    if reuse:runner.after_initialize=callback
    else:runner.before_initialize=callback
    result=await reach_ops(bootstrap)
    result=observed[0] if reuse else result
    assert 'runtime owner changed' in result['error']
    assert service.journal.get('native-verify-owner-loss' if reuse else 'ops-work-request-01') is None
