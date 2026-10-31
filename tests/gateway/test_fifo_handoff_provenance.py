"""FIFO dispatch credentials are task-local, one-use and never ingress metadata."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.fifo_handoff import (
    claim_fifo_head, consume_fifo_head, fifo_dispatch_scope, stage_fifo_head,
)
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


def lane(name="fifo"):
    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    adapter.gateway_runner = runner
    key = "agent:main:discord:group:" + name
    event = MessageEvent(text="selected", source=SessionSource(Platform.DISCORD, name, chat_type="group"))
    guard = asyncio.Event()
    guard._hermes_run_generation = 7
    adapter._active_sessions[key] = guard
    adapter._session_tasks[key] = asyncio.current_task()
    stage_fifo_head(runner, adapter, key, event)
    assert adapter._pending_messages.pop(key) is event
    return runner, adapter, key, event, guard


@pytest.mark.asyncio
async def test_native_head_claim_and_consumption_are_once_only_and_not_replayable():
    runner, adapter, key, event, guard = lane()
    with fifo_dispatch_scope(adapter, event, key, guard):
        head = claim_fifo_head(runner, event)
        assert head is not None and claim_fifo_head(runner, event) is None
        assert consume_fifo_head(runner, adapter, key, head)
        assert not consume_fifo_head(runner, adapter, key, head)
    assert not consume_fifo_head(runner, adapter, key, head)
    with fifo_dispatch_scope(adapter, event, key, guard):
        assert claim_fifo_head(runner, event) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [True, {"fifo_head": True}, "_gateway_fifo_promoted=True"])
async def test_metadata_and_untyped_attribute_cannot_claim_head(payload):
    runner, adapter, key, event, guard = lane()
    event.metadata.update(_gateway_fifo_promoted=payload, gateway_fifo_head=payload)
    event._gateway_fifo_head = payload
    with fifo_dispatch_scope(adapter, event, key, guard):
        assert claim_fifo_head(runner, event) is None


@pytest.mark.asyncio
async def test_copying_a_real_ticket_to_another_event_cannot_claim_it():
    runner, adapter, key, event, guard = lane()
    copied = replace(event)
    copied._gateway_fifo_head = event._gateway_fifo_head
    with fifo_dispatch_scope(adapter, copied, key, guard):
        assert claim_fifo_head(runner, copied) is None
    with fifo_dispatch_scope(adapter, event, key, guard):
        assert claim_fifo_head(runner, event) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["key", "runner", "adapter", "owner", "guard", "generation", "native-generation"])
async def test_dispatch_cannot_cross_session_owner_guard_or_generation(change):
    runner, adapter, key, event, guard = lane()
    with fifo_dispatch_scope(adapter, event, key, guard):
        head = claim_fifo_head(runner, event)
        assert head is not None
        selected_runner = object() if change == "runner" else runner
        selected_adapter = object() if change == "adapter" else adapter
        selected_key = "foreign" if change == "key" else key
        mutations = {
            "owner": lambda: adapter._session_tasks.__setitem__(key, object()),
            "guard": lambda: adapter._active_sessions.__setitem__(key, asyncio.Event()),
            "generation": lambda: setattr(guard, "_hermes_run_generation", 8),
            "native-generation": lambda: runner._invalidate_session_run_generation(key, reason="offline reset"),
        }
        if change in mutations:
            mutations[change]()
        assert not consume_fifo_head(selected_runner, selected_adapter, selected_key, head)


@pytest.mark.asyncio
async def test_inherited_context_in_child_task_confers_no_head_authority():
    runner, adapter, key, event, guard = lane()
    with fifo_dispatch_scope(adapter, event, key, guard):
        head = claim_fifo_head(runner, event)

        async def child():
            assert claim_fifo_head(runner, event) is None
            assert not consume_fifo_head(runner, adapter, key, head)

        await asyncio.create_task(child())
        assert consume_fifo_head(runner, adapter, key, head)


@pytest.mark.asyncio
async def test_parallel_sessions_have_independent_one_use_scopes():
    both = asyncio.Event()
    entered = []

    async def process(name):
        runner, adapter, key, event, guard = lane(name)
        with fifo_dispatch_scope(adapter, event, key, guard):
            head = claim_fifo_head(runner, event)
            entered.append(name)
            if len(entered) == 2:
                both.set()
            await both.wait()
            assert consume_fifo_head(runner, adapter, key, head)
            assert not consume_fifo_head(runner, adapter, key, head)
        assert claim_fifo_head(runner, event) is None

    await asyncio.wait_for(asyncio.gather(process("default"), process("ops")), timeout=5)


@pytest.mark.asyncio
async def test_cancelled_scope_cannot_leave_a_reusable_head():
    entered, hold = asyncio.Event(), asyncio.Event()
    captured = []

    async def process():
        runner, adapter, key, event, guard = lane()
        try:
            with fifo_dispatch_scope(adapter, event, key, guard):
                head = claim_fifo_head(runner, event)
                captured.append((runner, adapter, key, event, guard, head))
                entered.set()
                await hold.wait()
        except asyncio.CancelledError:
            assert claim_fifo_head(runner, event) is None
            assert not consume_fifo_head(runner, adapter, key, head)
            raise

    task = asyncio.create_task(process())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runner, adapter, key, event, guard, head = captured[0]
    adapter._session_tasks[key] = asyncio.current_task()
    assert not consume_fifo_head(runner, adapter, key, head)
    with fifo_dispatch_scope(adapter, event, key, guard):
        assert claim_fifo_head(runner, event) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", [False, True])
async def test_fresh_ingress_still_rescues_old_orphans(forged):
    runner, adapter, key, fresh, guard = lane()
    fresh._gateway_fifo_head = True if forged else None
    fresh.metadata["_gateway_fifo_promoted"] = True
    older = replace(fresh, text="older", metadata={})
    runner._session_state(key).conversation.queued_events.append(older)
    with fifo_dispatch_scope(adapter, fresh, key, guard):
        head = claim_fifo_head(runner, fresh)
        chosen, _, _ = runner._hm_rescue_orphaned_fifo(fresh, fresh.source, False, key, fifo_head=head)
    assert chosen is older
    assert adapter._pending_messages[key] is fresh
    assert not runner._overflow_queue(key)


@pytest.mark.asyncio
async def test_runner_in_band_dequeue_discards_unused_ticket():
    runner, adapter, key, event, guard = lane()
    stage_fifo_head(runner, adapter, key, event)
    assert adapter.get_pending_message(key) is event
    with fifo_dispatch_scope(adapter, event, key, guard):
        assert claim_fifo_head(runner, event) is None


@pytest.mark.asyncio
async def test_staging_cannot_claim_a_different_adapter_runner():
    runner, adapter, key, event, guard = lane()
    adapter.gateway_runner = SimpleNamespace()
    with fifo_dispatch_scope(adapter, event, key, guard):
        assert claim_fifo_head(runner, event) is None
