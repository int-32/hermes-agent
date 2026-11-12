"""Profile stop / ``gateway restart`` against a managed turn whose worker already left the registry.

``execute_managed``'s ``finally`` pops the worker from ``_managed_workers`` and then runs
``worker.close`` (``closed`` is set first, the reap takes up to seconds) before the turn settles, while
``authority.adopted`` still holds that ``ManagedWorker`` for the running generation.
``join_authority_work`` calls ``stop_authority_turns(in_process=True)`` every 50ms through that
window; each call must neither raise (``managed_worker_lost`` on a closed worker, backpressure on a
re-signalled one, ``TypeError`` from the zero-arg ``interrupt()``) nor leave a live worker unstopped:
any of those aborted every restart while a managed turn was finishing."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_authority_cancel_settlement import _authority, _submit

# More passes than ManagedWorker.commands holds (16): a re-signalling stop would fill it.
JOIN_PASSES = 20


async def _stop_out_of_registry_worker(tmp_path, monkeypatch, prepare):
    from gateway import session_finite
    from gateway.run_runtime import stop_authority_turns
    from gateway.session_managed_worker import ManagedWorker

    db, authority = _authority(tmp_path, monkeypatch)
    authority.runner._cached_agent_for = {}.get
    adopted, stopped = asyncio.Event(), asyncio.Event()
    holder = {}

    async def execute(authority, ref, row):
        worker = holder['worker'] = ManagedWorker(SimpleNamespace())
        authority.adopt_agent(ref.session_id, row['generation'], worker)
        prepare(worker)
        adopted.set()
        await asyncio.wait_for(stopped.wait(), 5)
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        await _submit(authority, 'managed')
        await asyncio.wait_for(adopted.wait(), 5)
        assert 's' not in getattr(authority, '_managed_workers', {})
        for _ in range(JOIN_PASSES):
            stop_authority_turns(authority, in_process=True, reason='gateway restart')
        stopped.set()
        await asyncio.wait_for(authority.sessions['s'].task, 5)
    worker = holder['worker']
    frames = []
    while not worker.commands.empty():
        frames.append(worker.commands.get_nowait())
    return worker, frames


@pytest.mark.asyncio
async def test_live_out_of_registry_worker_is_stopped_once_with_the_cause(tmp_path, monkeypatch):
    worker, frames = await _stop_out_of_registry_worker(tmp_path, monkeypatch, lambda worker: None)
    assert worker.stop.is_set(), 'the profile Stop must latch the managed worker'
    assert frames == [{'type': 'stop'}], 'repeated join passes must not re-signal a stopped worker'
    assert worker.stop_cause == 'gateway restart'


@pytest.mark.asyncio
async def test_closing_out_of_registry_worker_is_left_to_its_close(tmp_path, monkeypatch):
    worker, frames = await _stop_out_of_registry_worker(tmp_path, monkeypatch, lambda worker: worker.closed.set())
    assert frames == []
    assert worker.stop_cause is None


@pytest.mark.asyncio
async def test_user_stopped_out_of_registry_worker_keeps_no_restart_cause(tmp_path, monkeypatch):
    worker, frames = await _stop_out_of_registry_worker(
        tmp_path, monkeypatch, lambda worker: worker.control({'type': 'stop'}))
    assert frames == [{'type': 'stop'}], 'only the user Stop is queued'
    assert worker.stop_cause is None, "the user's Stop came first"
