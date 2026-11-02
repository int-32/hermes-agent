"""Refusal and replay contracts use the actual private dispatcher and SessionStore."""
import asyncio
import json
from pathlib import Path

import pytest

from gateway.agent_coordination import submit_document
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_task_source import read_sources
from tests.gateway.test_agent_handoff import native_owner
from tests.gateway import test_coordination_task_bootstrap as bootstrap_fixtures

bootstrap = bootstrap_fixtures.bootstrap

pytestmark=pytest.mark.platforms('posix')


async def reach_ops(fixture):
    service,doc,_sources,entries=fixture
    service.runner._gateway_loop=asyncio.get_running_loop()
    await submit_document(service,entries['business'].session_key,entries['business'].session_id,
                          service.home,'business-root-01','business',str(doc),'main')
    await service.tick()
    await service.tick()
    return service.runner.tool_replies[-1]


@pytest.mark.asyncio
async def test_identical_initialization_reuses_card_session_and_native_execution(bootstrap):
    service,_,_,_=bootstrap;runner=service.runner;runner.initialization_repeats=2
    reply=await reach_ops(bootstrap)
    assert reply['initialized'] and reply['status']=='queued'
    assert runner.tool_replies[-2]==runner.tool_replies[-1]
    assert len(read_sources(service.home))==2 and len(runner.session_store._entries)==4
    assert len(service.journal.children('business-peer-01'))==1 and not runner.actions
    for _ in range(6):await service.tick()
    assert len(runner.actions)==1
    assert service.journal.get('business-root-01')['status']=='delivered'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['business-id','wrong-parent','wrong-guild','wrong-profile','archived-thread',
                                  'not-thread','document-drift','execution-revoked','owner-revoked','smuggled-target'])
async def test_refusal_happens_before_new_card_or_session(bootstrap,fault):
    service,doc,_,_=bootstrap;runner=service.runner
    before=read_sources(service.home)
    original=runner.channel
    async def change(_runner,_event):
        changes={'business-id':{'work_task_id':'business'}, 'smuggled-target':{'target':'peer'}}
        runner.initialization_changes.update(changes.get(fault,{}))
        actions={
            'document-drift':lambda: doc.write_text('changed after real parent admission'),
            'execution-revoked':lambda: service.policy.update(execution_enabled=False),
            'owner-revoked':lambda: setattr(runner,'allowed',False),
            'wrong-profile':lambda: setattr(runner.config,'profile_routes',runner.config.profile_routes[:1]),
        }
        if fault in actions:
            actions[fault]()
        async def channel(tid):
            value=await original(tid)
            if fault=='wrong-parent':
                from gateway.profile_routing import ProfileRoute
                runner.config.profile_routes.append(ProfileRoute('explicit-thread','discord','aiyu-ops',thread_id='201'))
                value.parent_id=999
            edits={'wrong-guild':(value.guild,'id',98), 'archived-thread':(value,'archived',True),
                   'not-thread':(value.type,'name','text')}
            if fault in edits:
                setattr(*edits[fault])
            return value
        runner.channel=channel;runner.adapter._resolve_channel=channel
    runner.before_initialize=change
    result=await reach_ops(bootstrap)
    assert 'error' in result,result
    assert read_sources(service.home)==before
    assert len(runner.session_store._entries)==3
    assert service.journal.get('ops-work-request-01') is None
    assert not runner.actions


@pytest.mark.asyncio
async def test_unbound_caller_cannot_bootstrap_by_passing_the_real_main_sid(bootstrap):
    from tools.registry import registry
    import tools.agent_handoff_tool
    service,_,_,entries=bootstrap
    before=read_sources(service.home)
    result=json.loads(registry.dispatch('coordinate_session',dict(action='initialize_task',request_id='unbound-bootstrap-01',
        work_task_id='business-ops',thread_id='201'),session_id=entries['ops'].session_id))
    assert 'bound native gateway model turn' in result['error']
    assert read_sources(service.home)==before and len(service.runner.session_store._entries)==3


@pytest.mark.asyncio
async def test_same_business_parent_guard_is_not_relaxed_for_arbitrary_work_documents(bootstrap):
    from tools.registry import registry
    service,doc,_,_=bootstrap;observed=[]
    async def after(runner,event):
        work_doc=doc.parents[1]/'business-ops/HANDOFF.md';work_doc.parent.mkdir();work_doc.write_text('unrelated work scope')
        sid=runner.session_store.lookup_by_session_key(runner._session_key_for_source(event.source)).session_id
        result=json.loads(await asyncio.to_thread(registry.dispatch,'coordinate_session',dict(request_id='unrelated-root-guard',
            task_id='business-ops',handoff_path=str(work_doc),target='task:business-ops'),session_id=sid))
        observed.append(result)
    service.runner.after_initialize=after
    await reach_ops(bootstrap)
    assert 'dependency parent is not an active native request' in observed[0]['error']
    assert service.journal.get('unrelated-root-guard') is None
    assert service.journal.get('ops-work-request-01')['document']['task_id']=='business'


@pytest.mark.asyncio
async def test_reset_of_new_work_session_refuses_old_queued_execution(bootstrap):
    service,_,_,_=bootstrap
    await reach_ops(bootstrap)
    spec=read_sources(service.home)['business-ops']
    entry=service.runner.session_store.lookup_by_session_key(spec['session_key'])
    with service.runner._profile_scope_for_source(entry.origin):
        reset=service.runner.session_store.get_or_create_session(entry.origin,force_new=True)
    assert reset.session_id!=spec['session_id']
    with pytest.raises(PermissionError,match='accepted request left the native compression publication'):
        await service.pinned_routes(service.journal.get('ops-work-request-01'))
    assert not service.runner.actions


@pytest.mark.asyncio
async def test_paused_work_refuses_queued_but_allows_its_already_generated_private_result(bootstrap):
    from gateway.coordination_receipts import run_request
    service,_,_,_=bootstrap
    await reach_ops(bootstrap)
    record=service.journal.get('ops-work-request-01')
    _,target,record=await service.pinned_routes(record)
    generated=await run_request(service,record,target)
    assert generated['status']=='generated'
    work=read_sources(service.home)['business-ops']
    with kbc.connect_closing(board='registry') as conn:kb.block_task(conn,work['native_id'],reason='User pause')
    await service.pinned_routes(generated)
    for _ in range(6):await service.tick()
    assert service.journal.get('ops-work-request-01')['status']=='delivered'
    assert service.journal.get('business-root-01')['status']=='delivered'
    assert len(service.runner.actions)==1


@pytest.mark.asyncio
async def test_paused_new_work_never_runs_queued_input(bootstrap):
    service,_,_,_=bootstrap
    await reach_ops(bootstrap)
    work=read_sources(service.home)['business-ops']
    with kbc.connect_closing(board='registry') as conn:kb.block_task(conn,work['native_id'],reason='User pause')
    with pytest.raises(PermissionError,match='closed or paused'):
        await service.pinned_routes(service.journal.get('ops-work-request-01'))
    assert not service.runner.actions


@pytest.mark.asyncio
async def test_existing_unbound_ops_route_is_not_adopted(bootstrap):
    service,_,_,_=bootstrap;runner=service.runner
    async def before(_runner,_event):
        from gateway.session import SessionSource
        from gateway.config import Platform
        source=SessionSource(Platform.DISCORD,'201',chat_type='thread',thread_id='201',parent_chat_id='400',user_id='42',scope_id='99')
        runner._canonicalize(source)
        with runner._profile_scope_for_source(source):runner.session_store.get_or_create_session(source)
    runner.before_initialize=before
    result=await reach_ops(bootstrap)
    assert 'unbound existing work session cannot be adopted' in result['error']
    assert len(read_sources(service.home))==1 and not runner.actions


@pytest.mark.asyncio
async def test_stable_work_id_cannot_move_to_a_different_native_thread(bootstrap):
    from tools.registry import registry
    service,_,_,_=bootstrap;observed=[]
    async def after(runner,event):
        sid=runner.session_store.lookup_by_session_key(runner._session_key_for_source(event.source)).session_id
        result=json.loads(await asyncio.to_thread(registry.dispatch,'coordinate_session',
            dict(action='initialize_task',request_id='different-thread-request',work_task_id='business-ops',thread_id='202'),session_id=sid))
        observed.append(result)
    service.runner.after_initialize=after
    await reach_ops(bootstrap)
    assert 'different task, route or closed goal' in observed[0]['error']
    assert len(read_sources(service.home))==2 and len(service.runner.session_store._entries)==4
    assert len(service.journal.children('business-peer-01'))==1


@pytest.mark.asyncio
async def test_owner_revoked_after_initialization_refuses_queued_execution(bootstrap):
    service,_,_,_=bootstrap
    await reach_ops(bootstrap)
    service.runner.allowed=False
    with pytest.raises(PermissionError,match='authorization'):
        await service.pinned_routes(service.journal.get('ops-work-request-01'))
    assert not service.runner.actions


@pytest.mark.asyncio
async def test_card_accept_failure_leaves_an_inert_real_session_and_retry_refuses_adoption(bootstrap,monkeypatch):
    service,_,_,_=bootstrap
    def fail(*args,**kwargs):
        raise OSError('isolated card writer failure')
    monkeypatch.setattr('hermes_cli.kanban_task_source.accept_task',fail)
    service.runner.initialization_repeats=2
    result=await reach_ops(bootstrap)
    assert 'unbound existing work session cannot be adopted' in result['error']
    assert 'isolated card writer failure' in service.runner.tool_replies[-2]['error']
    assert len(read_sources(service.home))==1 and len(service.runner.session_store._entries)==4
    assert service.journal.get('ops-work-request-01') is None and not service.runner.actions
