"""A consumed dependency can continue the same task without another human input.

Real registry/tool, private receipt, canonical native routes and SQLite journal;
only the model/transport and user-owned task data are isolated fixtures.
"""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from gateway import agent_handoff as handoff
from gateway.coordination_receipts import DOCUMENT_TURN, dispatch_context
from gateway.internal_receipt import (
    capture_internal_delivery, capture_internal_response, event_job, finish_internal_receipt,
)
from gateway.platforms.base import SendResult
from gateway.platforms.event import ProcessingOutcome
from gateway.session_context import native_model_turn_scope
from tests.gateway.native_source_fixtures import produced_star
from tests.gateway.test_agent_handoff import native_owner

pytestmark = [pytest.mark.platforms('posix'), pytest.mark.usefixtures('isolated_entrypoint_bootstrap')]


@pytest.fixture
def stage(tmp_path, monkeypatch):
    import tools.agent_handoff_tool
    value = produced_star(tmp_path, monkeypatch)
    value.service.runner._served_profile_for_home = lambda home: 'default'
    value.service.runner.entries = value.entries
    stage = SimpleNamespace(service=value.service, path=value.path, calls=[], responses=[], intents=[], busy=set(), probes=[])
    yield stage
    value.store.close_all_db_handles()


async def tool(stage, key, args, *, job=None):
    from tools.registry import registry
    service = stage.service
    service.runner._gateway_loop = asyncio.get_running_loop()
    entry = service.runner.entries[key]
    actual_key = service.runner._session_key_for_source(entry.origin)
    with native_model_turn_scope(service.runner, entry.origin, actual_key, entry.session_id, job=job):
        raw = await asyncio.to_thread(registry.dispatch, 'coordinate_session', args,
                                      session_id=entry.session_id)
    return json.loads(raw)


async def begin(stage, request='stage-root-0001'):
    result = await tool(stage, 'default-task', {'request_id': request, 'task_id': 'task-one',
        'handoff_path': str(stage.path), 'target': 'main'})
    assert result['status'] == 'queued', result
    return request


def install_handler(stage, *, after_intent=None):
    async def handle(event):
        turn = DOCUMENT_TURN.get()
        key = event.source.chat_id
        stage.calls.append((turn.work['request_id'], key, turn.phase))
        # The isolated scope handler reads the actual accepted document and returns its nonce.
        response = 'handled ' + turn.work['document']['sha256']
        if turn.phase == 'request':
            data = stage.path.read_bytes()
            actual = hashlib.sha256(data).hexdigest()
            assert actual == turn.work['document']['sha256']
            stage.probes.append({'request_id': turn.work['request_id'], 'sha256': actual, 'nonce': data.decode()})
            response = 'handled ' + data.decode()
        # Consume the actual result before ACK; a return is not a new user input.
        if turn.phase == 'origin':
            assert turn.child['response'].startswith('handled ')
            stage.responses.append(turn.child['response'])
        ack = await tool(stage, key, {'action': 'complete', 'request_id': turn.work['request_id'],
                                     'outcome': 'completed'}, job=event_job(event))
        assert ack['status'] == 'recorded', ack
        if key == 'default-task' and turn.work['request_id'] == 'stage-root-0001':
            stage.path.write_text('next authorized scope: read the second offline nonce')
            old = await tool(stage, key, {'request_id': turn.work['request_id'], 'task_id': 'task-one',
                'handoff_path': str(stage.path), 'target': 'main'}, job=event_job(event))
            assert old == {'error': 'receipt continuation cannot dispatch a loop or dispatch after completion'}
            print(json.dumps({'original_guard_refusal': old}))
            args = {'action': 'next_stage', 'request_id': turn.work['request_id'],
                    'handoff_path': str(stage.path)}
            result = await tool(stage, key, args, job=event_job(event))
            assert result.get('status') == 'recorded', result
            stage.intents.append(result)
            if after_intent:
                await after_intent(event, args)
        event._gateway_accepted = True
        capture_internal_response(event, response, successful=True)
        capture_internal_delivery(event, SendResult(success=True, message_id=key + '-' + turn.work['request_id']))
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)
    stage.service.runner.adapter.handle_message = handle


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', [False, True], ids=['idle', 'busy-with-fifo-head'])
async def test_receipt_consumed_then_second_handler_and_return_without_human(stage, busy, request):
    install_handler(stage)
    root_id = await begin(stage)
    await stage.service.tick()
    root = stage.service.journal.get(root_id)
    assert root['status'] == 'delivered'
    next_id = root['next_stage']['request_id']
    next_record = stage.service.journal.get(next_id)
    assert next_record['status'] == 'queued'
    assert next_record['stage_after_request_id'] == root_id
    assert next_record['document']['task_id'] == root['document']['task_id']
    assert next_record['document']['sha256'] != root['document']['sha256']
    assert next_record['root_request_id'] == next_id and not next_record.get('parent_request_id')
    # A different, pre-existing FIFO head must not be overtaken by the stage.
    if busy:
        stage.service.runner._queue_depth = lambda key, **kw: int(key in stage.busy)
        stage.busy.add(stage.service.runner.entries['default-main'].session_key)
        await stage.service.tick()
        assert len(stage.calls) == 2
        assert stage.service.journal.get(next_id)['status'] == 'queued'
        stage.busy.clear()
    await stage.service.tick()
    assert stage.service.journal.get(next_id)['status'] == 'delivered'
    assert stage.service.journal.get(next_id)['continuation_confirmed'] is True
    assert len(stage.responses) == 2 and stage.responses[0] != stage.responses[1]
    assert len(stage.probes) == 2 and stage.probes[0]['request_id'] == root_id
    assert stage.probes[1]['request_id'] == next_id
    assert stage.responses == ['handled ' + p['nonce'] for p in stage.probes]
    request.node.user_properties.append(('executed_scope_handlers', json.dumps(stage.probes, sort_keys=True)))
    request.node.user_properties.append(('actual_native_returns', json.dumps(stage.responses)))
    assert [key for _, key, _ in stage.calls] == [
        'default-main', 'default-task', 'default-main', 'default-task']
    await stage.service.tick()
    assert len(stage.calls) == 4


@pytest.mark.asyncio
async def test_duplicate_registration_and_restart_reconcile_one_committed_stage(stage):
    async def repeat(event, args):
        duplicate = await tool(stage, 'default-task', args, job=event_job(event))
        assert duplicate == stage.intents[-1]
        # Existing request is still prohibited inside origin, even after a valid stage intent.
        with pytest.raises(PermissionError, match='receipt continuation'):
            dispatch_context(stage.service.runner.entries['default-task'].session_key, 'not-a-stage-0001')
    install_handler(stage, after_intent=repeat)
    root_id = await begin(stage)
    await stage.service.tick()
    next_id = stage.service.journal.get(root_id)['next_stage']['request_id']
    restarted = handoff.HandoffService(stage.service.runner, stage.service.home, stage.service.policy)
    stage.service = restarted
    restarted.runner.service = restarted
    # A legitimate restart installs the actual new service; no native user input is added.
    handoff._service = restarted
    await restarted.tick()
    assert restarted.journal.get(next_id)['status'] == 'delivered'
    assert len(stage.calls) == 4 and len(restarted.journal.pending()) == 0


def change_authorization(stage, mutation):
    from gateway import status
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, projects_db
    from hermes_cli.kanban_task_source import read_sources, SOURCE_EVENT
    from hermes_state import SessionDB
    service = stage.service
    spec = read_sources(service.home)['task-one']
    def card():
        with kbc.connect_closing(board='registry') as db:
            operations = {
                'paused': lambda: kb.block_task(db, spec['native_id'], reason='isolated scope pause'),
                'closed': lambda: db.execute('UPDATE tasks SET status=? WHERE id=?', ('done', spec['native_id'])),
                'duplicate-source': lambda: kb._append_event(db, spec['native_id'], SOURCE_EVENT,
                    {**spec, 'task_id': 'another-task', 'session_key': 'another-source'}),
            }
            operations[mutation]()
            db.commit()
    def session():
        entry = service.runner.entries['default-task']
        with SessionDB(service.runner._resolve_profile_home_for_source(entry.origin) / 'state.db') as db:
            operations = {
                'ended': lambda: db.end_session(entry.session_id, 'session_reset'),
                'row-owner': lambda: db._write_sql('UPDATE sessions SET user_id=? WHERE id=?', ('other-owner', entry.session_id)),
            }
            operations[mutation]()
    def archive():
        with projects_db.connect_closing() as db:
            assert projects_db.archive_project(db, spec['project_id'])
    def reacquire():
        status.release_gateway_runtime_lock()
        assert status.acquire_gateway_runtime_lock()
    operations = {
        'paused': card, 'closed': card, 'duplicate-source': card,
        'project-archived': archive, 'ended': session, 'row-owner': session,
        'authorization': lambda: setattr(service.runner, '_is_user_authorized_for_source', lambda *a, **kw: False),
        'policy': lambda: service.policy['coordination'].update(enabled=False),
        'execution-consent': lambda: service.policy.update(execution_enabled=False),
        'document': lambda: stage.path.write_text('a different version after intent was recorded'),
        'owner-clear': lambda: handoff.clear_handoff_service(service.runner), 'owner-reacquire': reacquire,
    }
    operations[mutation]()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['paused', 'closed', 'duplicate-source', 'project-archived',
    'ended', 'row-owner', 'authorization', 'policy', 'execution-consent', 'document',
    'owner-clear', 'owner-reacquire'])
async def test_authorization_and_owner_rechecked_after_intent_before_publish(stage, mutation):
    async def revoke(event, args):
        change_authorization(stage, mutation)
    install_handler(stage, after_intent=revoke)
    root_id = await begin(stage)
    await stage.service.tick()
    root = stage.service.journal.get(root_id)
    assert stage.intents and stage.service.journal.get(stage.intents[0]['request_id']) is None
    assert len(stage.calls) == 2
    if mutation in {'paused', 'closed', 'project-archived', 'document', 'duplicate-source'}:
        assert root['status'] == 'delivered' and root['next_stage_error']
    if mutation.startswith('owner-'):
        assert root['status'] == 'ambiguous'


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['no-ack', 'same-scope', 'wrong-work', 'cross-task', 'cross-project',
                                   'claimed-target', 'claimed-task', 'legacy-unbound'])
async def test_no_authority_or_new_scope_can_be_claimed_by_tool_arguments(stage, defect):
    results = []
    async def handle(event):
        turn = DOCUMENT_TURN.get()
        key = event.source.chat_id
        if defect != 'no-ack' or turn.phase != 'origin':
            ack = await tool(stage, key, {'action': 'complete', 'request_id': turn.work['request_id'],
                                         'outcome': 'completed'}, job=event_job(event))
            assert ack['status'] == 'recorded'
        if turn.phase == 'origin':
            path = stage.path
            if defect != 'same-scope':
                path.write_text('second isolated stage scope')
            if defect in {'cross-task', 'cross-project'}:
                path = path.parent.parent / 'ops-one' / 'HANDOFF.md' if defect == 'cross-task' else (
                    path.parents[2] / 'other-project' / 'task-one' / 'HANDOFF.md')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('scope belonging elsewhere')
            if defect == 'legacy-unbound':
                turn.work['task_binding'] = None
                stage.service.journal.update(turn.work['request_id'], task_binding=None)
            args = {'action': 'next_stage', 'request_id': turn.work['request_id'], 'handoff_path': str(path)}
            if defect == 'wrong-work':
                args['request_id'] = 'unrelated-work-0001'
            if defect == 'claimed-target':
                args['target'] = 'peer'
            if defect == 'claimed-task':
                args['task_id'] = 'ops-one'
            results.append(await tool(stage, key, args, job=event_job(event)))
        event._gateway_accepted = True
        capture_internal_response(event, 'legitimate result', successful=True)
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)
    stage.service.runner.adapter.handle_message = handle
    root_id = await begin(stage)
    await stage.service.tick()
    assert len(results) == 1 and results[0].get('error'), results
    if defect == 'legacy-unbound':
        assert 'private task-origin' in results[0]['error']
    assert not stage.service.journal.get(root_id).get('next_stage')
    assert not stage.service.journal.pending()


@pytest.mark.asyncio
async def test_normal_turn_or_receipt_metadata_cannot_forge_next_stage(stage):
    response = await tool(stage, 'default-task', {'action': 'next_stage',
        'request_id': 'not-a-private-receipt', 'handoff_path': str(stage.path)})
    assert 'private task-origin' in response['error']
    assert stage.service.journal.pending() == []


@pytest.mark.asyncio
async def test_old_private_receipt_replay_cannot_reopen_next_stage(stage):
    saved = []
    async def remember(event, args):
        saved.append((event_job(event), args))
    install_handler(stage, after_intent=remember)
    await begin(stage)
    await stage.service.tick()
    replay = await tool(stage, 'default-task', saved[0][1], job=saved[0][0])
    assert 'current completed root return' in replay['error']
    await stage.service.tick()
    assert len(stage.calls) == 4


@pytest.mark.asyncio
async def test_changed_scope_cannot_register_a_second_successor_of_same_receipt(stage):
    async def different(event, args):
        stage.path.write_text('a different third scope cannot replace a recorded intent')
        response = await tool(stage, 'default-task', args, job=event_job(event))
        assert 'different next scope' in response['error']
    install_handler(stage, after_intent=different)
    root_id = await begin(stage)
    await stage.service.tick()
    root = stage.service.journal.get(root_id)
    assert root['status'] == 'delivered' and root['next_stage_error']
    assert stage.service.journal.get(root['next_stage']['request_id']) is None


@pytest.mark.asyncio
async def test_lost_intent_reply_reconciles_durable_record_without_resubmitting(stage, monkeypatch):
    original = stage.service.journal.update_many
    writes = []
    def lose_reply(changes, **kwargs):
        result = original(changes, **kwargs)
        if any('next_stage' in values for values in changes.values()) and not writes:
            writes.append(True)
            raise TimeoutError('isolated lost reply after durable intent COMMIT')
        return result
    monkeypatch.setattr(stage.service.journal, 'update_many', lose_reply)
    install_handler(stage)
    await begin(stage)
    await stage.service.tick()
    await stage.service.tick()
    assert writes == [True] and len(stage.calls) == 4


@pytest.mark.asyncio
async def test_lost_publish_reply_reconciles_both_committed_rows_without_reenqueue(stage, monkeypatch):
    original = stage.service.journal.update_many
    writes = []
    def lose_reply(changes, **kwargs):
        result = original(changes, **kwargs)
        if kwargs.get('successor') is not None:
            writes.append(kwargs['successor']['request_id'])
            raise TimeoutError('isolated lost reply after atomic return/stage COMMIT')
        return result
    monkeypatch.setattr(stage.service.journal, 'update_many', lose_reply)
    install_handler(stage)
    root_id = await begin(stage)
    await stage.service.tick()
    root = stage.service.journal.get(root_id)
    assert root['status'] == 'delivered'
    await stage.service.tick()
    assert writes == [root['next_stage']['request_id']] and len(stage.calls) == 4


@pytest.mark.asyncio
async def test_interrupted_origin_with_intent_cannot_publish_new_stage(stage):
    from gateway.internal_receipt import invalidate_internal_receipt
    async def interrupt(event, args):
        invalidate_internal_receipt(event)
    install_handler(stage, after_intent=interrupt)
    root_id = await begin(stage)
    await stage.service.tick()
    root = stage.service.journal.get(root_id)
    assert root['status'] == 'failed'
    assert stage.service.journal.get(root['next_stage']['request_id']) is None
    await stage.service.tick()
    assert len(stage.calls) == 2


@pytest.mark.asyncio
async def test_request_receiver_cannot_use_stage_to_dispatch_an_ancestor(stage):
    results = []
    async def handle(event):
        turn = DOCUMENT_TURN.get()
        key = event.source.chat_id
        ack = await tool(stage, key, {'action': 'complete', 'request_id': turn.work['request_id'],
                                     'outcome': 'completed'}, job=event_job(event))
        assert ack['status'] == 'recorded'
        if turn.phase == 'request':
            stage.path.write_text('new version still cannot bypass the ancestor edge')
            results.append(await tool(stage, key, {'action': 'next_stage', 'request_id': turn.work['request_id'],
                'handoff_path': str(stage.path)}, job=event_job(event)))
            results.append(await tool(stage, key, {'request_id': 'illegal-loop-0001', 'task_id': 'task-one',
                'handoff_path': str(stage.path), 'target': 'task:task-one'}, job=event_job(event)))
        event._gateway_accepted = True
        capture_internal_response(event, 'received scope', successful=True)
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)
    stage.service.runner.adapter.handle_message = handle
    root_id = await begin(stage)
    await stage.service.tick()
    assert all(r.get('error') for r in results) and len(results) == 2
    assert 'recursive coordination loop' in results[1]['error']
    assert stage.service.journal.get(root_id)['status'] == 'delivered'
    assert not stage.service.journal.get(root_id).get('next_stage')


@pytest.mark.asyncio
async def test_later_stage_cannot_replay_an_ancestor_document_scope(stage):
    old_bytes = stage.path.read_bytes()
    install_handler(stage)
    root_id = await begin(stage)
    await stage.service.tick()
    next_id = stage.service.journal.get(root_id)['next_stage']['request_id']
    rejected = []
    normal = stage.service.runner.adapter.handle_message
    async def handle(event):
        await normal(event)
        turn = DOCUMENT_TURN.get()
        if turn.phase == 'origin' and turn.work['request_id'] == next_id:
            stage.path.write_bytes(old_bytes)
            rejected.append(await tool(stage, 'default-task', {'action': 'next_stage', 'request_id': next_id,
                'handoff_path': str(stage.path)}, job=event_job(event)))
    stage.service.runner.adapter.handle_message = handle
    await stage.service.tick()
    assert 'ancestor scope replay' in rejected[0]['error']
    assert stage.service.journal.get(next_id)['status'] == 'delivered'
    assert not stage.service.journal.pending() and len(stage.calls) == 4


@pytest.mark.asyncio
async def test_revocation_during_successor_insert_rolls_back_stage_but_keeps_legal_old_return(stage, monkeypatch):
    from contextlib import contextmanager
    journal = stage.service.journal
    original = journal.connect
    observations = []
    @contextmanager
    def interleave():
        with original() as db:
            class Connection:
                def execute(self, sql, *args):
                    result = db.execute(sql, *args)
                    if sql.startswith('INSERT INTO requests') and str(args[0][0]).startswith('stage-'):
                        with original() as reader:
                            root = json.loads(reader.execute('SELECT record FROM requests WHERE id=?',
                                ('stage-root-0001',)).fetchone()[0])
                            next_row = reader.execute('SELECT record FROM requests WHERE id=?', (args[0][0],)).fetchone()
                        observations.append((root['status'], next_row))
                        change_authorization(stage, 'closed')
                    return result
            yield Connection()
    async def install(event, args):
        monkeypatch.setattr(journal, 'connect', interleave)
    install_handler(stage, after_intent=install)
    root_id = await begin(stage)
    await stage.service.tick()
    root = journal.get(root_id)
    assert observations == [('returning', None)]
    assert root['status'] == 'delivered' and root['next_stage_error']
    assert journal.get(root['next_stage']['request_id']) is None


@pytest.mark.asyncio
async def test_cancel_after_real_atomic_commit_then_restart_executes_successor_once(stage, monkeypatch):
    import threading
    from contextlib import contextmanager
    committed, release = threading.Event(), threading.Event()
    journal = stage.service.journal
    original = journal.connect
    @contextmanager
    def after_commit():
        inserted = False
        with original() as db:
            class Connection:
                def execute(self, sql, *args):
                    nonlocal inserted
                    result = db.execute(sql, *args)
                    inserted |= sql.startswith('INSERT INTO requests') and str(args[0][0]).startswith('stage-')
                    return result
            yield Connection()
        if inserted:
            committed.set()
            assert release.wait(10), 'test must release the committed writer'
    async def install(event, args):
        monkeypatch.setattr(journal, 'connect', after_commit)
    install_handler(stage, after_intent=install)
    root_id = await begin(stage)
    tick = asyncio.create_task(stage.service.tick())
    try:
        assert await asyncio.to_thread(committed.wait, 10)
        # An independent connection sees both rows after actual COMMIT, before caller reply.
        root = journal.get(root_id)
        next_id = root['next_stage']['request_id']
        assert root['status'] == 'delivered' and journal.get(next_id)['status'] == 'queued'
        tick.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await tick
    finally:
        release.set()
        await asyncio.gather(tick, return_exceptions=True)
    restarted = handoff.HandoffService(stage.service.runner, stage.service.home, stage.service.policy)
    handoff._service = restarted
    stage.service = restarted
    await restarted.tick()
    await restarted.tick()
    assert restarted.journal.get(next_id)['status'] == 'delivered' and len(stage.calls) == 4


@pytest.mark.asyncio
async def test_failed_atomic_commit_does_not_leave_successor_and_is_not_replayed(stage, monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    journal = stage.service.journal
    original = journal.connect
    @contextmanager
    def refuse_commit():
        inserted = False
        with original() as db:
            class Connection:
                def execute(self, sql, *args):
                    nonlocal inserted
                    result = db.execute(sql, *args)
                    inserted |= sql.startswith('INSERT INTO requests') and str(args[0][0]).startswith('stage-')
                    return result
            yield Connection()
            if inserted:
                raise sqlite3.OperationalError('isolated real transaction rollback before COMMIT')
    async def install(event, args):
        monkeypatch.setattr(journal, 'connect', refuse_commit)
    install_handler(stage, after_intent=install)
    root_id = await begin(stage)
    await stage.service.tick()
    root = journal.get(root_id)
    assert root['status'] == 'ambiguous'
    assert journal.get(root['next_stage']['request_id']) is None
    restarted = handoff.HandoffService(stage.service.runner, stage.service.home, stage.service.policy)
    handoff._service = restarted
    stage.service = restarted
    await restarted.tick()
    assert len(stage.calls) == 2 and not restarted.journal.pending()


@pytest.mark.asyncio
async def test_owner_replaced_before_first_stage_call_cannot_adopt_old_receiving_turn(stage, monkeypatch):
    from gateway import status
    original = handoff.continue_coordination
    switched = []
    def replace_before_call(*args):
        status.release_gateway_runtime_lock()
        assert status.acquire_gateway_runtime_lock()
        switched.append(True)
        return original(*args)
    monkeypatch.setattr(handoff, 'continue_coordination', replace_before_call)
    install_handler(stage)
    root_id = await begin(stage)
    await stage.service.tick()
    root = stage.service.journal.get(root_id)
    assert switched and not root.get('next_stage')
    assert len(stage.calls) == 2


@pytest.mark.asyncio
async def test_unknown_native_return_keeps_intent_unpublished_without_replay(stage, monkeypatch):
    import sys
    original_finish, original_wait = finish_internal_receipt, asyncio.wait_for
    def lose_return(event, outcome):
        if DOCUMENT_TURN.get().phase != 'origin':
            original_finish(event, outcome)
    async def deadline(awaitable, timeout):
        if timeout == 900 and not awaitable.done():
            raise TimeoutError('isolated unknown native receipt, no successful return')
        return await original_wait(awaitable, timeout)
    monkeypatch.setattr(sys.modules[__name__], 'finish_internal_receipt', lose_return)
    monkeypatch.setattr(asyncio, 'wait_for', deadline)
    install_handler(stage)
    root_id = await begin(stage)
    await stage.service.tick()
    root = stage.service.journal.get(root_id)
    assert root['status'] == 'ambiguous' and 'unknown' in root['error']
    assert stage.service.journal.get(root['next_stage']['request_id']) is None
    await stage.service.tick()
    assert len(stage.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(('order', 'lost_reply'), [('return-first', False), ('intent-first', False), ('return-first', True)])
async def test_bridge_deadline_serializes_late_intent_and_return_then_restarts_once(stage, monkeypatch, order, lost_reply):
    import threading
    journal = stage.service.journal
    original = journal.update_many
    intent_entered, release_intent, intent_done = threading.Event(), threading.Event(), threading.Event()
    return_entered, release_return = threading.Event(), threading.Event()
    results, observations = [], []
    def interleave(changes, **kwargs):
        if any('next_stage' in v for v in changes.values()):
            intent_entered.set()
            assert release_intent.wait(25)
            try:
                result = original(changes, **kwargs)
                if lost_reply:
                    raise TimeoutError('unknown reply after real intent/successor COMMIT')
                return result
            finally:
                intent_done.set()
        if order == 'intent-first' and any(v.get('status') == 'delivered' for v in changes.values()):
            return_entered.set()
            assert release_return.wait(25)
        return original(changes, **kwargs)
    monkeypatch.setattr(journal, 'update_many', interleave)
    async def handle(event):
        turn = DOCUMENT_TURN.get(); key = event.source.chat_id
        stage.calls.append((turn.work['request_id'], turn.phase))
        if turn.phase == 'request':
            data = stage.path.read_bytes()
            assert hashlib.sha256(data).hexdigest() == turn.work['document']['sha256']
            stage.probes.append(data.decode())
            response = 'handled ' + data.decode()
        else:
            stage.responses.append(turn.child['response'])
            assert turn.child['response'] == 'handled ' + stage.probes[-1]
            response = 'consumed ' + turn.child['response']
        ack = await tool(stage, key, {'action': 'complete', 'request_id': turn.work['request_id'],
            'outcome': 'completed'}, job=event_job(event))
        assert ack['status'] == 'recorded'
        if turn.phase == 'origin' and turn.work['request_id'] == 'stage-root-0001':
            stage.path.write_text('second scope after actual bridge deadline')
            results.append(await tool(stage, key, {'action': 'next_stage', 'request_id': turn.work['request_id'],
                'handoff_path': str(stage.path)}, job=event_job(event)))
        event._gateway_accepted = True
        capture_internal_response(event, response, successful=True)
        capture_internal_delivery(event, SendResult(success=True, message_id='final-' + key))
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)
    stage.service.runner.adapter.handle_message = handle
    rid = await begin(stage)
    tick = asyncio.create_task(stage.service.tick())
    try:
        assert await asyncio.to_thread(intent_entered.wait, 10)
        if order == 'return-first':
            await asyncio.wait_for(asyncio.shield(tick), 16)
            before = journal.get(rid)
            assert before['status'] == 'delivered' and not before.get('next_stage')
        else:
            assert await asyncio.to_thread(return_entered.wait, 16)
            assert journal.get(rid)['status'] == 'returning'
        assert results == [{'error': ''}]  # Unchanged real ten-second bound-tool deadline.
        release_intent.set()
        assert await asyncio.to_thread(intent_done.wait, 10)
        # Join the server operation, including reconciliation of an unknown COMMIT reply.
        async with stage.service.coordination_lock:
            pass
        middle = journal.get(rid)
        nid = middle['next_stage']['request_id']
        if order == 'intent-first':
            assert middle['status'] == 'returning' and journal.get(nid) is None
        else:
            assert middle['status'] == 'delivered' and journal.get(nid)['status'] == 'queued'
        observations.append(middle)
        release_return.set()
        await asyncio.wait_for(tick, 10)
        root = journal.get(rid)
        assert root['status'] == 'delivered' and journal.get(nid)['status'] == 'queued'
        restarted = handoff.HandoffService(stage.service.runner, stage.service.home, stage.service.policy)
        handoff._service = restarted; stage.service = restarted; restarted.runner.service = restarted
        await restarted.tick(); await restarted.tick()
        assert restarted.journal.get(nid)['status'] == 'delivered'
        assert len(stage.calls) == 4 and len(set(stage.probes)) == 2
        assert stage.responses == ['handled ' + value for value in stage.probes]
        assert len(restarted.journal.children(rid)) == 0  # Successor is a new stage, not an old dependency replay.
        assert len(observations) == 1
    finally:
        release_intent.set(); release_return.set()
        await asyncio.gather(tick, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['before-commit', 'after-commit'])
async def test_cancel_intent_operation_joins_writer_before_successful_return(stage, monkeypatch, when):
    import threading
    from gateway import coordination_stages as stages
    entered, release = threading.Event(), threading.Event()
    journal = stage.service.journal
    original_update, original_call = journal.update_many, stages._durable_call
    operation = []
    async def capture(function, *args):
        if function is stages._write_intent:
            operation.append(asyncio.current_task())
        return await original_call(function, *args)
    def barrier(changes, **kwargs):
        is_intent = any('next_stage' in values for values in changes.values())
        if is_intent and when == 'before-commit':
            entered.set(); assert release.wait(20)
        result = original_update(changes, **kwargs)
        if is_intent and when == 'after-commit':
            entered.set(); assert release.wait(20)
        return result
    monkeypatch.setattr(stages, '_durable_call', capture)
    monkeypatch.setattr(journal, 'update_many', barrier)
    results = []
    async def handle(event):
        turn = DOCUMENT_TURN.get(); key = event.source.chat_id
        stage.calls.append((turn.work['request_id'], turn.phase))
        result = await tool(stage, key, {'action': 'complete', 'request_id': turn.work['request_id'],
            'outcome': 'completed'}, job=event_job(event))
        assert result['status'] == 'recorded'
        if turn.phase == 'origin' and turn.work['request_id'] == 'stage-root-0001':
            stage.path.write_text('new scope with a cancelled server reply')
            results.append(await tool(stage, key, {'action': 'next_stage', 'request_id': turn.work['request_id'],
                'handoff_path': str(stage.path)}, job=event_job(event)))
        event._gateway_accepted = True
        capture_internal_response(event, 'actual completed result', successful=True)
        capture_internal_delivery(event, SendResult(success=True, message_id='final-' + key))
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)
    async def cancel():
        assert await asyncio.to_thread(entered.wait, 10)
        assert operation
        operation[0].cancel()
        release.set()
    stage.service.runner.adapter.handle_message = handle
    rid = await begin(stage)
    cancelling = asyncio.create_task(cancel())
    try:
        await asyncio.wait_for(stage.service.tick(), 20)
        await cancelling
        root = journal.get(rid); nid = root['next_stage']['request_id']
        assert results and 'error' in results[0]
        assert root['status'] == 'delivered' and journal.get(nid)['status'] == 'queued'
        await stage.service.tick(); await stage.service.tick()
        assert journal.get(nid)['status'] == 'delivered' and len(stage.calls) == 4
    finally:
        release.set()
        await asyncio.gather(cancelling, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('committed', [False, True], ids=['before-commit', 'lost-commit-reply'])
async def test_no_intent_return_reconciles_unknown_commit_without_replaying(stage, monkeypatch, committed):
    journal = stage.service.journal
    original = journal.update_many
    failures = []
    def unknown_reply(changes, **kwargs):
        if any(values.get('status') == 'delivered' for values in changes.values()) and not failures:
            failures.append(True)
            if committed:
                original(changes, **kwargs)
            raise TimeoutError('unknown native return commit result')
        return original(changes, **kwargs)
    monkeypatch.setattr(journal, 'update_many', unknown_reply)
    install_handler(stage)
    rid = await begin(stage, request='return-without-intent')
    await stage.service.tick()
    root = journal.get(rid)
    assert root['status'] == ('delivered' if committed else 'ambiguous')
    assert not root.get('next_stage') and len(stage.calls) == 2 and failures == [True]
    await stage.service.tick()
    assert len(stage.calls) == 2
