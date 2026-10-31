"""Two real private producers share a loop without sharing receiving identity or ACKs."""
import asyncio

import pytest

from gateway.agent_coordination import submit_document
from gateway.coordination_receipts import DOCUMENT_TURN, run_request
from gateway.internal_receipt import capture_internal_response, finish_internal_receipt, receipt_for
from gateway.native_endpoint import NATIVE_TURN, run_native_model
from gateway.platforms.event import ProcessingOutcome
from tests.gateway import test_agent_handoff as owner_fixtures
from tests.gateway.native_source_fixtures import produced_star

native_owner = owner_fixtures.native_owner
pytestmark = pytest.mark.platforms('posix')


@pytest.mark.asyncio
async def test_parallel_received_jobs_keep_distinct_source_profile_and_ack(tmp_path, monkeypatch):
    value = produced_star(tmp_path, monkeypatch)
    service, runner = value.service, value.service.runner
    runner._gateway_loop = asyncio.get_running_loop()
    incoming = asyncio.Queue()
    both = asyncio.Event()
    active, observed = [], []

    async def receive(event):
        event._gateway_accepted = True
        await incoming.put(event)

    async def model(message, prompt, history, source, sid, **kw):
        native = NATIVE_TURN.get()
        current = native.job.document_turn
        active.append((native, current))
        if len(active) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        assert NATIVE_TURN.get() is native and native.key == current.key
        args = dict(action='complete', request_id=current.work['request_id'], outcome='completed')
        from gateway.native_endpoint import native_request
        from gateway.session_context import _SESSION_NATIVE_SOURCE, _SESSION_NATIVE_HOME
        result = await native_request(service, native.key, sid, _SESSION_NATIVE_HOME.get(),
            _SESSION_NATIVE_SOURCE.get(), coordination=True, completion=True, args=(args['request_id'], args['outcome']))
        observed.append((current, result))
        return dict(final_response='private final', api_calls=1)

    async def consume():
        event = await incoming.get()
        assert DOCUMENT_TURN.get() is None and NATIVE_TURN.get() is None
        key = runner._session_key_for_source(event.source)
        sid = value.entries[event.source.chat_id].session_id
        result = await run_native_model(runner, event.text, '', [], event.source, sid,
            dict(session_key=key, native_job=receipt_for(event).job))
        assert DOCUMENT_TURN.get() is None and NATIVE_TURN.get() is None
        capture_internal_response(event, result['final_response'], successful=True)
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)

    runner._run_agent_inner = model
    runner.adapter.handle_message = receive
    consumers = [asyncio.create_task(consume()) for _ in range(2)]
    producers = []
    try:
        for name, task in [('default', 'task-one'), ('ops', 'ops-one')]:
            entry = value.entries[name + '-main']
            path = value.path.parent.parent / task / 'HANDOFF.md'
            path.parent.mkdir(exist_ok=True)
            path.write_text('isolated private completion')
            await submit_document(service, entry.session_key, entry.session_id,
                runner._resolve_profile_home_for_source(entry.origin), 'parallel-' + name,
                task, str(path), 'task:' + task)
            record = service.journal.get('parallel-' + name)
            target = value.entries[name + '-task'].origin
            producers.append(asyncio.create_task(run_request(service, record, target)))
        results = await asyncio.wait_for(asyncio.gather(*producers, *consumers), 15)
        assert {r['status'] for r in results[:2]} == {'generated'}
        assert {r['outcome'] for r in results[:2]} == {'completed'}, observed
        assert len(active) == 2 and active[0][0] is not active[1][0] and active[0][1] is not active[1][1]
        assert active[0][0].home != active[1][0].home
        for turn, result in observed:
            assert result['status'] == 'recorded' and turn.completion == 'completed'
            ack, = service.journal.get(turn.work['request_id'])['acknowledgements']
            assert ack['session_key'] == turn.key and ack['session_id'] == turn.sid
    finally:
        for task in [*producers, *consumers]:
            task.cancel()
        await asyncio.gather(*producers, *consumers, return_exceptions=True)
        value.store.close_all_db_handles()
