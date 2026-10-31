"""Private producers must reach an independently created model consumer, not its ambient turn."""
import asyncio
import json

import pytest

from gateway.internal_receipt import capture_internal_response, finish_internal_receipt, receipt_for
from gateway.platforms.event import ProcessingOutcome
from tests.gateway import test_active_native_endpoint as model_fixtures

live_model = model_fixtures.live_model
shared_native = model_fixtures.shared_native

pytestmark = [pytest.mark.platforms('posix'), pytest.mark.usefixtures('isolated_entrypoint_bootstrap')]


@pytest.mark.asyncio
@pytest.mark.parametrize('compressed', [False, True])
async def test_independent_private_receiver_completes_same_producer_work(live_model, monkeypatch, compressed):
    from gateway.coordination_receipts import DOCUMENT_TURN
    from gateway.native_endpoint import NATIVE_TURN
    from gateway.agent_coordination import submit_document

    model = live_model
    service, runner = model.service, model.runner
    service.policy['execution_enabled'] = True
    incoming = asyncio.Queue()
    observed, receiving = [], []
    model.plan.transition = 'compression' if compressed else 'none'

    async def receive(event):
        event._gateway_accepted = True
        await incoming.put(event)

    async def consume():
        while True:
            event = await incoming.get()
            # This consumer existed before either producer; no Context clearing or copied SID.
            assert DOCUMENT_TURN.get() is None and NATIVE_TURN.get() is None
            entry = await runner.async_session_store.lookup_by_session_key(runner._session_key_for_source(event.source))
            key, sid = entry.session_key, entry.session_id
            envelope = json.loads(event.text.splitlines()[1])
            rid = envelope.get('work_request_id', envelope['request_id'])
            model.plan.args = {'action': 'complete', 'request_id': rid, 'outcome': 'completed'}
            model.plan.calls = 0
            model.plan.keys.add(key)
            generation = runner._begin_session_run_generation(key)
            try:
                job = getattr(receipt_for(event), 'job', None)
                kwargs = {'native_job': job} if job is not None else {}
                actual = getattr(job, 'document_turn', None)
                if actual is not None:
                    assert actual.work['request_id'] == rid and actual.completion is None
                    if actual.phase == 'continuation':
                        assert actual.child['request_id'] == envelope['request_id'] != rid
                    receiving.append((actual, job.lineage))
                result = await runner._run_agent(event.text, '', [], event.source, sid,
                    session_key=key, run_generation=generation, **kwargs)
                observed.append(model.observed[-1]['result'])
                if actual is not None and observed[-1].get('status') == 'recorded':
                    assert actual.completion == 'completed'
                elif actual is not None:
                    assert actual.completion is None
                capture_internal_response(event, result['final_response'], successful=True)
                finish_internal_receipt(event, ProcessingOutcome.SUCCESS)
            finally:
                runner._release_running_agent_state(key, run_generation=generation)
                incoming.task_done()

    adapter = runner._delivery_adapter_for(model.entry.origin)
    monkeypatch.setattr(adapter, 'handle_message', receive)
    consumer = asyncio.create_task(consume())
    try:
        await submit_document(service, model.entry.session_key, model.entry.session_id, service.home,
            'independent-private', 'task-one', str(model.path), 'main')
        for _ in range(5):
            tick = asyncio.create_task(service.tick())
            done, _ = await asyncio.wait({tick, consumer}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
            assert done, 'isolated producer/consumer did not finish'
            if consumer in done:
                await consumer
            await tick
            await asyncio.wait_for(incoming.join(), 5)
            if service.journal.get('independent-private')['status'] == 'delivered':
                break
        expected = ['recorded', 'recorded']
        assert [x.get('status') for x in observed] == expected, observed
        work = service.journal.get('independent-private')
        assert work['status'] == 'delivered' and work['outcome'] == 'completed'
        assert {x['phase'] for x in work['acknowledgements']} == {'request', 'origin'}
        assert len(receiving) == len(expected)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
