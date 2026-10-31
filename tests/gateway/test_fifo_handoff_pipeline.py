"""FIFO producer -> Base task handoff -> real admitted ingress, without network I/O."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.internal_receipt import InternalTurnReceipt
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from tests.gateway.test_queued_followup_processing_hooks import _install_fake_agent
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


@pytest.fixture
def fifo_pipeline(monkeypatch, tmp_path):
    calls, arrivals, resolved, authorized, enqueued = [], [], [], [], []
    sending, release_send = asyncio.Event(), asyncio.Event()
    head_admitted, release_head = asyncio.Event(), asyncio.Event()
    source = SessionSource(Platform.DISCORD, "fifo-controlled", chat_type="group")
    key = "agent:main:discord:group:fifo-controlled"
    plan = {}

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, **kwargs):
            calls.append(message)
            return {"final_response": "answer-" + message, "messages": [], "api_calls": 1}

    class Adapter(ProgressCaptureAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            if content == "answer-N":
                sending.set()
                await release_send.wait()
            await super().send(chat_id, content, reply_to, metadata)
            return SendResult(success=True, message_id="final-" + str(len(self.sent)))

    _install_fake_agent(monkeypatch, tmp_path, Agent)
    adapter = Adapter(platform=Platform.DISCORD)
    adapter.config.typing_indicator = False
    runner = _make_runner(adapter)
    adapter.gateway_runner = runner
    runner._update_runtime_status = lambda *args, **kwargs: None
    runner._persist_active_agents = lambda: None
    runner._run_post_turn_hooks = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    runner._admit_bot_message_for_source = lambda source: True

    def authorize(current_source):
        authorized.append(current_source.user_id)
        return current_source.user_id != "denied"

    runner._is_user_authorized_for_source = authorize
    admit = runner._hm_admit_event

    async def pause_after_admission(incoming):
        result = await admit(incoming)
        if incoming.text == "U2" and plan.get("late_head"):
            head_admitted.set()
            await release_head.wait()
        return result

    runner._hm_admit_event = pause_after_admission
    lookup_event = None

    def lookup(session_key):
        assert session_key == key
        if lookup_event == "W" and plan["reject"] == "exception":
            raise RuntimeError("offline lookup failure")
        return SimpleNamespace(session_id="current-sid", session_key=key)

    runner.session_store.lookup_by_session_key = lookup

    def event(text, *, internal=False, reject=None):
        return MessageEvent(text=text, source=source, internal=internal, metadata={
            "gateway_session_key": key if reject != "route" else "changed-route",
            "gateway_session_id": "expired-sid" if reject == "sid" else "current-sid",
            "gateway_session_strict": True})

    def enqueue(incoming):
        enqueued.append(incoming.text)
        runner._queue_or_replace_pending_event(key, incoming)

    async def model_turn(incoming, current_source, session_key, generation):
        nonlocal lookup_event
        lookup_event = incoming.text
        resolved.append(incoming.text)
        # This is the native SID/route/heartbeat resolver, not an admission stub.
        if await runner._hmwa_resolve_session(incoming, current_source) is None:
            return None
        if incoming.text == "N":
            enqueue(event("W", internal=True, reject=plan["reject"]))
            if plan["arrival"] == "existing":
                for queued in plan["users"]:
                    enqueue(queued)
        result = await runner._run_agent(message=incoming.text, context_prompt="", history=[],
                                        source=current_source, session_id="current-sid", session_key=key,
                                        run_generation=generation)
        return await runner._hmwa_deliver_turn_response(
            incoming, current_source, SimpleNamespace(session_id="current-sid"), key, None,
            result, [], result["final_response"], None, False)

    # Keep the whole real ingress (including auth, commands, orphan rescue and generation).
    # Only the model preparation boundary is reduced; native session resolution still runs.
    runner._handle_message_with_agent = model_turn

    async def handler(incoming):
        arrivals.append(incoming.text)
        return await runner._handle_message(incoming)

    adapter._message_handler = handler

    async def run(*, count=2, reject="sid", arrival="existing", late_u4=False, mixed=False,
                  late_head=False, head_reject=None, rewrite=False):
        users = [event("U" + str(index + 2), internal=mixed and index == 1) for index in range(count)]
        if head_reject:
            users[0] = event("U2", reject=head_reject)
            if head_reject == "auth":
                users[0].source = replace(source, user_id="denied")
        if rewrite:
            from hermes_cli.plugins import get_plugin_manager
            manager = get_plugin_manager()
            manager._discovered = True
            manager._hooks.setdefault("pre_gateway_dispatch", []).append(
                lambda **kwargs: {"action": "rewrite", "text": "U2-edited"}
                if kwargs["event"].text == "U2" else None)
        plan.update(reject=reject, arrival=arrival, users=users, late_head=late_head)
        first = event("N", internal=True)
        first._internal_turn_receipt = InternalTurnReceipt(
            asyncio.get_running_loop().create_future(), {"id": "bot:peer", "is_bot": True})
        assert adapter._start_session_processing(first, key)
        try:
            await asyncio.wait_for(sending.wait(), timeout=5)
            if arrival == "final-send":
                for queued in users:
                    enqueue(queued)
            if late_u4:
                enqueue(event("U4"))
            release_send.set()
            if late_head:
                await asyncio.wait_for(head_admitted.wait(), timeout=5)
                enqueue(event("U4"))
                release_head.set()
            for _ in range(12):
                tasks = list(adapter._background_tasks)
                if not tasks:
                    break
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert not adapter._background_tasks
        finally:
            release_send.set()
            release_head.set()
            await adapter.cancel_background_tasks()
        return SimpleNamespace(calls=calls, arrivals=arrivals, resolved=resolved, authorized=authorized,
                               enqueued=enqueued, adapter=adapter, runner=runner, key=key,
                               receipt=first._internal_turn_receipt,
                               overflow=list(runner._overflow_queue(key) or ()))

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", ["sid", "route", "exception"])
async def test_rejected_wake_preserves_two_human_fifo_heads(fifo_pipeline, reject):
    result = await fifo_pipeline(reject=reject)
    assert result.enqueued == ["W", "U2", "U3"]
    assert result.calls == ["N", "U2", "U3"]
    assert not result.overflow and not result.adapter._pending_messages
    assert not result.adapter._session_tasks and not result.adapter._active_sessions


@pytest.mark.asyncio
async def test_late_u4_during_selected_head_admission_cannot_jump_u3(fifo_pipeline):
    result = await fifo_pipeline(late_head=True)
    assert result.enqueued == ["W", "U2", "U3", "U4"]
    assert result.calls == ["N", "U2", "U3", "U4"]
    assert not result.overflow and not result.adapter._pending_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("count", range(5))
@pytest.mark.parametrize("reject", [None, "sid", "route", "exception"])
@pytest.mark.parametrize("arrival", ["existing", "final-send"])
async def test_short_fifo_matrix_matches_enqueue_order(fifo_pipeline, count, reject, arrival):
    result = await fifo_pipeline(count=count, reject=reject, arrival=arrival)
    users = ["U" + str(index + 2) for index in range(count)]
    expected = ["N"] + (["W"] if reject is None else []) + users
    assert result.enqueued == ["W", *users]
    assert result.calls == expected
    assert not result.overflow and not result.adapter._pending_messages
    assert not result.adapter._session_tasks and not result.adapter._active_sessions
    assert result.receipt.future.result() == {
        "response": "answer-N", "generated": True, "delivered": True, "message_ids": ["final-1"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [None, "sid", "route", "exception"])
@pytest.mark.parametrize("arrival", ["existing", "final-send"])
async def test_late_u4_during_native_final_send_keeps_fifo(fifo_pipeline, reject, arrival):
    result = await fifo_pipeline(count=2, reject=reject, arrival=arrival, late_u4=True)
    assert result.enqueued == ["W", "U2", "U3", "U4"]
    assert result.calls == ["N"] + (["W"] if reject is None else []) + ["U2", "U3", "U4"]
    assert not result.overflow and not result.adapter._pending_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [None, "sid", "exception"])
@pytest.mark.parametrize("arrival", ["existing", "final-send"])
async def test_mixed_internal_and_human_suffix_keeps_enqueue_order(fifo_pipeline, reject, arrival):
    result = await fifo_pipeline(count=4, reject=reject, arrival=arrival, mixed=True)
    assert result.calls == ["N"] + (["W"] if reject is None else []) + ["U2", "U3", "U4", "U5"]
    assert not result.overflow and not result.adapter._pending_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("head_reject", ["sid", "route", "auth"])
async def test_selected_fifo_head_still_passes_real_native_admission(fifo_pipeline, head_reject):
    result = await fifo_pipeline(count=3, head_reject=head_reject)
    assert result.calls == ["N", "U3", "U4"]
    assert result.arrivals == ["N", "W", "U2", "U3"]
    if head_reject == "auth":
        assert "denied" in result.authorized and "U2" not in result.resolved
    else:
        assert "U2" in result.resolved
    assert not result.overflow and not result.adapter._pending_messages


@pytest.mark.asyncio
async def test_native_plugin_text_rewrite_retains_selected_head_order(fifo_pipeline):
    result = await fifo_pipeline(rewrite=True)
    assert result.enqueued == ["W", "U2", "U3"]
    assert result.calls == ["N", "U2-edited", "U3"]
    assert "U2-edited" in result.resolved
    assert not result.overflow and not result.adapter._pending_messages
