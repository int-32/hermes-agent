"""Real /stop, busy await, Base orphan recovery and admitted FIFO ingress."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from tests.gateway.test_queued_followup_processing_hooks import _install_fake_agent
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


@pytest.fixture
def busy_idle_pipeline(monkeypatch, tmp_path):
    calls, admissions, transitions = [], [], []
    started, cancelled, finish_owner = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_turn = asyncio.Event()
    busy_waiting, resume_busy = asyncio.Event(), asyncio.Event()
    source = SessionSource(Platform.DISCORD, "busy-idle-controlled", chat_type="group")
    key = "agent:main:discord:group:busy-idle-controlled"

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, **kwargs):
            calls.append(message)
            return {"final_response": "answer-" + message, "messages": [], "api_calls": 1}

    _install_fake_agent(monkeypatch, tmp_path, Agent)
    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    adapter.config.typing_indicator = False
    runner = _make_runner(adapter)
    adapter.gateway_runner = runner
    runner._update_runtime_status = lambda *args, **kwargs: None
    runner._persist_active_agents = lambda: None
    runner._run_post_turn_hooks = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    runner._admit_bot_message_for_source = lambda source: True
    runner._is_user_authorized_for_source = lambda source: True
    runner._effective_busy_input_mode = lambda source: "queue"
    runner._effective_busy_text_mode = lambda source: "interrupt"
    runner._compose_busy_ack_message = lambda *args, **kwargs: "queued"
    runner.session_store.lookup_by_session_key = lambda current: SimpleNamespace(
        session_id="current-sid", session_key=current)

    def event(text):
        return MessageEvent(text=text, source=source, metadata={
            "gateway_session_key": key, "gateway_session_id": "current-sid",
            "gateway_session_strict": True})

    async def model_turn(incoming, current_source, session_key, generation):
        if await runner._hmwa_resolve_session(incoming, current_source) is None:
            return None
        if incoming.text == "N":
            started.set()
            await release_turn.wait()
            return None
        result = await runner._run_agent(
            message=incoming.text, context_prompt="", history=[], source=current_source,
            session_id="current-sid", session_key=key, run_generation=generation)
        return await runner._hmwa_deliver_turn_response(
            incoming, current_source, SimpleNamespace(session_id="current-sid"), key, None,
            result, [], result["final_response"], None, False)

    runner._handle_message_with_agent = model_turn

    async def handler(incoming):
        admissions.append(incoming.text)
        return await runner._handle_message(incoming)

    async def processing_hook(name, incoming, *args):
        if name == "on_processing_complete" and incoming.text == "N":
            cancelled.set()
            await finish_owner.wait()

    approval = runner._route_plaintext_approval_while_busy

    async def pause_busy(incoming, session_key):
        if incoming.text == "E":
            transitions.append("busy-await")
            busy_waiting.set()
            await resume_busy.wait()
        return await approval(incoming, session_key)

    adapter._message_handler = handler
    adapter._run_processing_hook = processing_hook
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    runner._route_plaintext_approval_while_busy = pause_busy

    async def run(*, count=1, handoff="base-released"):
        stop_task = intake_task = None
        try:
            await adapter.handle_message(event("N"))
            await asyncio.wait_for(started.wait(), timeout=5)
            owner = adapter._session_tasks[key]
            runner._queue_or_replace_pending_event(key, event("discarded-head"))
            users = ["X" + str(i + 1) for i in range(count)]
            for user in users:
                runner._queue_or_replace_pending_event(key, event(user))
            if handoff in {"runner-released", "live-owner"}:
                # Native runner /stop may release its turn before Base finishes.
                await handler(event("/stop"))
            else:
                stop_task = asyncio.create_task(adapter.handle_message(event("/stop")))
                await asyncio.wait_for(cancelled.wait(), timeout=5)
            assert key not in adapter._pending_messages
            assert [e.text for e in runner._overflow_queue(key)] == users
            assert not owner.done() and key in adapter._active_sessions
            if handoff == "cold-after-stop":
                finish_owner.set()
                await asyncio.wait_for(stop_task, timeout=5)
                assert owner.done() and key not in adapter._active_sessions
                transitions.append("cold-after-stop")
                await adapter.handle_message(event("E"))
            else:
                intake_task = asyncio.create_task(adapter.handle_message(event("E")))
                await asyncio.wait_for(busy_waiting.wait(), timeout=5)
                if handoff in {"live-owner", "base-no-owner"}:
                    if handoff == "live-owner":
                        assert adapter._session_tasks[key] is owner and not owner.done()
                    else:
                        assert key not in adapter._session_tasks and not owner.done()
                    assert key in adapter._active_sessions
                    transitions.append(handoff)
                    resume_busy.set()
                    await asyncio.wait_for(intake_task, timeout=5)
                    release_turn.set()
                    finish_owner.set()
                    await asyncio.wait_for(stop_task if stop_task else owner, timeout=5)
                else:
                    release_turn.set()
                    finish_owner.set()
                    await asyncio.wait_for(stop_task if stop_task else owner, timeout=5)
                    assert owner.done() and key not in adapter._active_sessions
                    transitions.append("idle-after-owner-cleanup")
                    resume_busy.set()
                    await asyncio.wait_for(intake_task, timeout=5)
            for _ in range(count + 5):
                tasks = list(adapter._background_tasks)
                if not tasks:
                    break
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert not adapter._background_tasks
            assert not adapter._pending_messages and not runner._overflow_queue(key)
            assert not adapter._session_tasks and not adapter._active_sessions
            return SimpleNamespace(calls=calls, admissions=admissions, transitions=transitions,
                                   expected=[*users, "E"])
        finally:
            release_turn.set()
            finish_owner.set()
            resume_busy.set()
            for task in (stop_task, intake_task):
                if task is not None and not task.done():
                    task.cancel()
            await adapter.cancel_background_tasks()
            await asyncio.gather(*(t for t in (stop_task, intake_task) if t), return_exceptions=True)

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("count", range(5))
@pytest.mark.parametrize("handoff", ["base-released", "runner-released"])
async def test_stop_busy_await_owner_cleanup_keeps_old_orphans_before_fresh_input(
        busy_idle_pipeline, count, handoff):
    result = await busy_idle_pipeline(count=count, handoff=handoff)
    assert result.transitions == ["busy-await", "idle-after-owner-cleanup"]
    assert "/stop" in result.admissions and "discarded-head" not in result.calls
    assert result.calls == result.expected


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 4])
@pytest.mark.parametrize("handoff", ["live-owner", "base-no-owner", "cold-after-stop"])
async def test_stop_intake_uses_current_owner_and_guard_or_cold_rescue(busy_idle_pipeline, count, handoff):
    result = await busy_idle_pipeline(count=count, handoff=handoff)
    assert result.transitions == ([handoff] if handoff == "cold-after-stop" else ["busy-await", handoff])
    assert result.calls == result.expected


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("state", ["live", "no-guard", "no-owner", "done-owner", "successor"])
async def test_enqueue_prestages_only_with_current_live_base_owner_and_guard(state, count):
    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    adapter.config.typing_indicator = False
    runner = _make_runner(adapter)
    adapter.gateway_runner = runner
    source = SessionSource(Platform.DISCORD, "owner-boundary", chat_type="group")
    key = "agent:main:discord:group:owner-boundary"
    started = {name: asyncio.Event() for name in ("N", "S")}
    release = {name: asyncio.Event() for name in ("N", "S")}

    async def handler(incoming):
        started[incoming.text].set()
        await release[incoming.text].wait()

    adapter._message_handler = handler
    old = [MessageEvent(text="X" + str(i), source=source) for i in range(count)]
    fresh = MessageEvent(text="E", source=source)
    try:
        assert adapter._start_session_processing(MessageEvent(text="N", source=source), key)
        await asyncio.wait_for(started["N"].wait(), timeout=5)
        original_owner, original_guard = adapter._session_tasks[key], adapter._active_sessions[key]
        runner._session_state(key).conversation.queued_events.extend(old)
        async def no_guard():
            adapter._release_session_guard(key, guard=original_guard)

        async def no_owner():
            # cancel_session_processing removes ownership before awaiting unwind.
            adapter._session_tasks.pop(key)

        async def done_owner():
            release["N"].set()
            await asyncio.wait_for(original_owner, timeout=5)
            # Native guard-swap cleanup retains the old done task for stale-lock heal.
            adapter._active_sessions[key] = asyncio.Event()
            adapter._session_tasks[key] = original_owner
            assert original_owner.done()

        async def new_successor():
            assert adapter._start_session_processing(MessageEvent(text="S", source=source), key)
            await asyncio.wait_for(started["S"].wait(), timeout=5)
            successor = adapter._session_tasks[key]
            successor_guard = adapter._active_sessions[key]
            release["N"].set()
            await asyncio.wait_for(original_owner, timeout=5)
            assert adapter._session_tasks[key] is successor and not successor.done()
            assert adapter._active_sessions[key] is successor_guard is not original_guard

        async def unchanged():
            return None

        await {"live": unchanged, "no-guard": no_guard, "no-owner": no_owner,
               "done-owner": done_owner, "successor": new_successor}[state]()
        runner._enqueue_fifo(key, fresh, adapter)
        live = state in {"live", "successor"}
        assert adapter._pending_messages[key] is (old[0] if live else fresh)
        assert runner._overflow_queue(key) == ([*old[1:], fresh] if live else old)
        assert fresh._gateway_accepted is True
    finally:
        # Producer-only boundary tests stop before dispatching their pending events.
        adapter._pending_messages.clear()
        runner._session_state(key).conversation.queued_events.clear()
        for barrier in release.values():
            barrier.set()
        await adapter.cancel_background_tasks()
