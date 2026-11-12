"""Profile stop / ``gateway restart`` against a managed turn whose worker already left the registry.

``execute_managed``'s ``finally`` pops the worker from ``_managed_workers`` before ``worker.close``
returns and the turn settles, while ``authority.adopted`` still holds that ``ManagedWorker`` for the
running generation. ``stop_authority_turns(in_process=True)`` then hard-interrupts it like an
in-process agent; the zero-argument ``ManagedWorker.interrupt()`` must take that Stop instead of
raising ``TypeError`` (which aborted every restart while a managed turn was finishing)."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_authority_cancel_settlement import _authority, _submit


@pytest.mark.asyncio
async def test_profile_stop_reaches_adopted_managed_worker_outside_the_registry(tmp_path, monkeypatch):
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
        adopted.set()
        await asyncio.wait_for(stopped.wait(), 5)
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        await _submit(authority, 'managed')
        await asyncio.wait_for(adopted.wait(), 5)
        assert 's' not in getattr(authority, '_managed_workers', {})
        assert stop_authority_turns(authority, in_process=True, reason='gateway restart') == 1
        stopped.set()
        await asyncio.wait_for(authority.sessions['s'].task, 5)

    worker = holder['worker']
    assert worker.stop.is_set(), 'the profile Stop must latch the managed worker'
    assert worker.commands.get_nowait() == {'type': 'stop'}
