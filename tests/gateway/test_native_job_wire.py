"""Wire/model data cannot manufacture current-work associations; native scopes unwind exactly."""
import asyncio

import pytest

from gateway.internal_receipt import event_job
from gateway.native_endpoint import NATIVE_TURN
from gateway.platforms.event import MessageEvent
from tests.gateway import native_endpoint_fixtures as source_fixtures

shared_native = source_fixtures.shared_native
pytestmark = [pytest.mark.platforms('posix'), pytest.mark.usefixtures('isolated_entrypoint_bootstrap')]


def test_metadata_is_not_a_process_local_receiving_reference():
    spoof = dict(phase='request', request_id='old-work', owner_epoch=1, generation=1)
    event = MessageEvent(text='untrusted data', source=None, internal=True,
                         metadata={'native_job': spoof, '_native_claim': spoof})
    event._native_claim = spoof
    event._internal_turn_receipt = {'job': spoof}
    assert event_job(event) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['normal', 'error', 'cancel'])
async def test_invalid_reference_and_nested_models_restore_existing_scope(shared_native, monkeypatch, finish):
    service, _, _, entry = shared_native
    runner = service.runner
    seen = []
    entered = asyncio.Event()

    async def inner(message, *args, **kwargs):
        seen.append(NATIVE_TURN.get())
        if message == 'outer':
            outer = NATIVE_TURN.get()
            with pytest.raises(PermissionError, match='process-local receiving reference'):
                await runner._run_agent('spoof', '', [], entry.origin, entry.session_id,
                    session_key=entry.session_key, native_job={'document_turn': 'invented'})
            assert NATIVE_TURN.get() is outer
            try:
                await runner._run_agent('nested', '', [], entry.origin, entry.session_id,
                    session_key=entry.session_key)
            finally:
                assert NATIVE_TURN.get() is outer
            return 'outer done'
        assert NATIVE_TURN.get() is not seen[0] and NATIVE_TURN.get().job is None
        entered.set()
        if finish == 'error':
            raise ValueError('fake provider error')
        if finish == 'cancel':
            await asyncio.Future()
        return 'nested done'

    monkeypatch.setattr(runner, '_run_agent_inner', inner)
    before = NATIVE_TURN.get()
    task = asyncio.create_task(runner._run_agent('outer', '', [], entry.origin, entry.session_id,
        session_key=entry.session_key))
    if finish == 'cancel':
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif finish == 'error':
        with pytest.raises(ValueError, match='fake provider error'):
            await task
    else:
        assert await task == 'outer done'
    assert len(seen) == 2 and NATIVE_TURN.get() is before
