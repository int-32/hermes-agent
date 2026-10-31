"""Watcher/tool-worker contracts; real Base intake is in test_strict_compression_return."""
import asyncio
import contextlib
from functools import wraps
import json
import threading

import pytest

from gateway.coordination_receipts import DOCUMENT_TURN
from gateway.internal_receipt import capture_internal_response, finish_internal_receipt
from gateway.native_endpoint import NATIVE_TURN
from gateway.platforms.event import ProcessingOutcome
from tests.gateway import test_active_native_endpoint as model_fixtures

shared_native = model_fixtures.shared_native
live_model = model_fixtures.live_model
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


def configure_request(model, lane):
    model.plan.args = {"action": "request", "task_id": "task-one", "handoff_path": str(model.path),
                       "request_id": "publication-request", "target": "main"}


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["native"])
@pytest.mark.parametrize("finish", ["normal", "model_error", "service_recovery"])
async def test_independent_watcher_preserves_and_settles_original_request(live_model, monkeypatch, lane, finish):
    from agent import conversation_loop
    from gateway.agent_handoff import HandoffService

    model = live_model
    service, runner = model.service, model.runner
    configure_request(model, lane)
    service.policy["execution_enabled"] = True
    # Fixture bootstrap uses an isolated home, with the real ESTOP guard still enabled.
    from gateway.agent_handoff import is_engaged
    assert not is_engaged() and not runner._draining and not runner._external_drain_active
    old = model.entry.session_id
    arrived, release = asyncio.Event(), threading.Event()

    def hold_model():
        runner._gateway_loop.call_soon_threadsafe(arrived.set)
        assert release.wait(20), "test did not release the real model's final response"

    model.plan.after_tool = hold_model
    permits = asyncio.Queue()
    original_tick = service.tick

    async def gated_tick():
        done = await permits.get()
        assert NATIVE_TURN.get() is None and DOCUMENT_TURN.get() is None
        try:
            await original_tick()
        finally:
            done.set_result(None)

    async def poll():
        done = asyncio.get_running_loop().create_future()
        await permits.put(done)
        await asyncio.wait_for(done, 10)

    monkeypatch.setattr(service, "tick", gated_tick)
    # Created at startup, before the actual native scope/model task exists. No setter,
    # copied turn context, or ContextVar clearing is used to manufacture independence.
    watcher = asyncio.create_task(service.watch())
    running = asyncio.create_task(model.run())
    adapter = runner._delivery_adapter_for(model.entry.origin)
    events = []

    async def receive(event):
        event._gateway_accepted = True
        turn = DOCUMENT_TURN.get()
        events.append((event, turn))
        assert turn.work["request_id"] == "publication-request"
        # Receiving and origin acknowledgements also use real native models and
        # tool workers; especially after an exception left the entry unsynchronized.
        model.plan.calls, model.plan.transition, model.plan.after_tool = 0, "none", None
        model.plan.keys.add(turn.key)
        model.plan.args = {"action": "complete", "request_id": turn.work["request_id"], "outcome": "completed"}
        generation = runner._begin_session_run_generation(turn.key)
        try:
            await runner._run_agent(event.text, "", [], event.source, turn.sid, session_key=turn.key,
                                   run_generation=generation)
        finally:
            runner._release_running_agent_state(turn.key, run_generation=generation)
        assert model.observed[-1]["result"]["status"] == "recorded", model.observed[-1]
        capture_internal_response(event, "isolated native receipt", successful=True)
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)

    monkeypatch.setattr(adapter, "handle_message", receive)
    try:
        await asyncio.wait_for(arrived.wait(), 10)
        assert model.observed[0]["result"]["status"] == "queued", model.observed
        record = service.journal.get("publication-request")
        child = model.agents[0].session_id
        assert record["source_sid"] == child != old == model.entry.session_id
        assert model.observed[0]["sid"] == model.observed[0]["passed_sid"] == child
        assert model.windows[0]["tip"] == child
        adapter._active_sessions[record["target_key"]] = asyncio.Event()
        await poll()
        current = service.journal.get(record["request_id"])
        print(json.dumps({"lane": lane, "entry": old, "accepted": child,
                          "independent_watcher": True, "window_status": current["status"],
                          "error": current.get("error")}))
        assert current["status"] == "queued", current
        assert not events
        source_generation = runner._current_session_run_generation(record["source_key"])
        if finish == "normal":
            adapter._active_sessions[record["source_key"]] = asyncio.Event()
            adapter._active_sessions.pop(record["target_key"])
            await poll()
            assert not running.done() and model.entry.session_id == old
            assert service.journal.get(record["request_id"])["status"] == "generated"
            assert len(events) == 1, "source is busy; its original receipt must wait for release"
        finalize = conversation_loop.finalize_turn
        if finish != "normal":
            @wraps(finalize)
            def fail_after_tools(*a, **kw):
                raise RuntimeError("scripted model finalization failure after durable acceptance")
            monkeypatch.setattr(conversation_loop, "finalize_turn", fail_after_tools)
        release.set()
        if finish == "normal":
            await asyncio.wait_for(running, 10)
            assert model.entry.session_id == child
        else:
            with pytest.raises(RuntimeError, match="scripted model finalization"):
                await asyncio.wait_for(running, 10)
            assert model.entry.session_id == child, "failed run must publish its proven compression before native return"
        monkeypatch.setattr(conversation_loop, "finalize_turn", finalize)
        runner._release_running_agent_state(record["source_key"], run_generation=source_generation)
        assert NATIVE_TURN.get() is None and DOCUMENT_TURN.get() is None
        if finish == "service_recovery":
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            service = HandoffService(runner, service.home, service.policy)
            original_tick = service.tick
            monkeypatch.setattr(service, "tick", gated_tick)
            monkeypatch.setattr("gateway.agent_handoff._service", service)
            model.plan.service = service
            watcher = asyncio.create_task(service.watch())
        adapter._active_sessions.pop(record["target_key"], None)
        adapter._active_sessions.pop(record["source_key"], None)
        await poll()
        settled = service.journal.get(record["request_id"])
        assert settled["status"] == "delivered", settled
        for name in ("request_id", "source_key", "source_sid", "target_key", "origin", "caller_home"):
            assert settled[name] == record[name]
        assert len(events) == 2
        assert settled["root_request_id"] == record["request_id"]
        assert settled["continuation_confirmed"] and settled["return_session_id"] == child
        assert [t.phase for _, t in events] == ["request", "origin"]
        await poll()
        assert service.journal.get(record["request_id"])["status"] == "delivered"
        assert len(events) == 2
    finally:
        release.set()
        if not running.done():
            await asyncio.wait_for(running, 10)
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
