"""Native receipt boundaries preserve late interrupts, FIFO and gateway drain."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.internal_receipt import InternalTurnReceipt, receipt_for
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from tests.gateway.test_queued_followup_processing_hooks import _install_fake_agent
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


@pytest.fixture
def queue_turn(monkeypatch, tmp_path):
    """Real runner, orphan rescue, final send and processing reporter; fake model/transport."""
    calls, drains, events = [], [], []
    source = SessionSource(Platform.DISCORD, "controlled-chat", chat_type="group")
    key = "agent:main:discord:group:controlled-chat"

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, **kwargs):
            calls.append(message)
            return {"final_response": "answer-" + message, "messages": [], "api_calls": 1}

    class Adapter(ProgressCaptureAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            await super().send(chat_id, content, reply_to, metadata)
            return SendResult(success=True, message_id="message-" + str(len(self.sent)))

        def _spawn_drain_task(self, event, session_key, delay=0):
            drains.append((event, session_key))

    _install_fake_agent(monkeypatch, tmp_path, Agent)
    adapter = Adapter(platform=Platform.DISCORD)
    adapter.config.typing_indicator = False
    runner = _make_runner(adapter)
    runner._update_runtime_status = lambda *args, **kwargs: None
    window = {"value": None}
    original_finalize = runner._run_agent_finalize_streaming_tts

    async def finalize(*args):
        await original_finalize(*args)
        if calls == ["N"]:
            if window["value"] == "before-helper":
                await adapter.interrupt_session_activity(key, source.chat_id)
            elif window["value"] == "draining":
                runner._draining = True

    runner._run_agent_finalize_streaming_tts = finalize

    async def handler(incoming):
        # This is the actual ingress consumer that exposed the U2/U1 inversion.
        event, current_source, _ = runner._hm_rescue_orphaned_fifo(
            incoming, incoming.source, incoming.internal, key)
        runner._session_state(key).turn.event = event
        events.append(event)
        result = await runner._run_agent(message=event.text, context_prompt="", history=[],
                                        source=current_source, session_id="native-queue-sid", session_key=key)
        text = await runner._hmwa_deliver_turn_response(
            event, current_source, SimpleNamespace(session_id="native-queue-sid"), key, None,
            result, [], result["final_response"], None, False)
        if incoming.text == "N" and window["value"] == "after-helper":
            # Result/capture is already successful; the real adapter guard changes afterwards.
            await adapter.interrupt_session_activity(key, source.chat_id)
        return text

    adapter._message_handler = handler

    async def run(*, native=True, queued=(("W", True),), interrupt=None):
        window["value"] = interrupt
        first = MessageEvent(text="N", source=source, internal=native)
        if native:
            first._internal_turn_receipt = InternalTurnReceipt(
                asyncio.get_running_loop().create_future(), {"id": "bot:peer", "is_bot": True})
        for text, internal in queued:
            # Both busy queue mode and protected interrupt demotion use this native queue sink.
            runner._queue_or_replace_pending_event(key, MessageEvent(text=text, source=source, internal=internal))
        await adapter._process_message_background(first, key)
        initial_drains = list(drains)
        for _ in range(8):
            if not drains:
                break
            event, drain_key = drains.pop(0)
            await adapter._process_message_background(event, drain_key)
        assert not drains, "test must fully drain the bounded fixture"
        return SimpleNamespace(calls=calls, adapter=adapter, runner=runner, first=first,
                               receipt=receipt_for(first), initial_drains=initial_drains, events=events,
                               slot=dict(adapter._pending_messages), overflow=list(runner._overflow_queue(key) or ()))

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["before-helper", "after-helper"])
async def test_late_adapter_interrupt_cannot_report_a_suppressed_final_as_delivered(queue_turn, window):
    result = await queue_turn(interrupt=window)
    assert "answer-N" not in [send["content"] for send in result.adapter.sent]
    final = result.receipt.future.result()
    assert final["generated"] is False
    assert final["delivered"] is False
    assert final["message_ids"] == []
    assert result.calls == ["N", "W"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_real_slot_and_orphan_rescue_preserve_human_fifo(queue_turn, native):
    result = await queue_turn(native=native, queued=(("U1", False), ("U2", False)))
    assert result.calls == ["N", "U1", "U2"]
    assert [send["content"] for send in result.adapter.sent] == ["answer-N", "answer-U1", "answer-U2"]
    assert not result.slot and not result.overflow


@pytest.mark.asyncio
async def test_internal_notification_and_overflow_are_neither_lost_nor_duplicated(queue_turn):
    result = await queue_turn(queued=(("W1", True), ("W2", True), ("U1", False)))
    assert result.calls == ["N", "W1", "W2", "U1"]
    assert [send["content"] for send in result.adapter.sent] == [
        "answer-N", "answer-W1", "answer-W2", "answer-U1"]
    assert not result.slot and not result.overflow
    assert result.receipt.future.result() == {
        "response": "answer-N", "generated": True, "delivered": True, "message_ids": ["message-1"]}


@pytest.mark.asyncio
async def test_gateway_drain_does_not_dispatch_a_parked_notification(queue_turn):
    result = await queue_turn(interrupt="draining")
    assert result.calls == ["N"]
    assert result.initial_drains == []
    assert not result.slot and not result.overflow
