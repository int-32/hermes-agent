"""A native receipt final must settle before unrelated queued notifications run."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.coordination_receipts import DOCUMENT_TURN, complete_turn
from gateway.config import Platform
from gateway.internal_receipt import (
    InternalTurnReceipt, capture_internal_delivery, capture_internal_response,
    drain_pending_turn, finish_internal_receipt, invalidate_internal_receipt, receipt_for,
)
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.event import ProcessingOutcome
from gateway.session import SessionSource
from tests.gateway.test_agent_coordination import make_star, send
from tests.gateway.test_queued_followup_processing_hooks import _install_fake_agent
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


@pytest.mark.asyncio
async def test_native_origin_final_settles_before_a_blocked_notification(monkeypatch, tmp_path):
    service, path = star = make_star(tmp_path, monkeypatch)
    visible = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    calls, drains, tasks, receipts = [], [], [], []

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, **kwargs):
            calls.append(message)
            if message != "native-origin":
                assert release.wait(10), "test must release the notification worker"
            return {"final_response": "native-final" if message == "native-origin" else "notification-final",
                    "messages": [], "api_calls": 1}

    class Adapter(ProgressCaptureAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            await super().send(chat_id, content, reply_to, metadata)
            if content == "native-final":
                visible.set()
            return SendResult(success=True, message_id="native-msg" if content == "native-final" else "notification-msg")

        def _spawn_drain_task(self, event, key, delay=0):
            drains.append((event, key))

    _install_fake_agent(monkeypatch, tmp_path, Agent)
    adapter = Adapter(platform=Platform.DISCORD)
    adapter.config.typing_indicator = False
    runner = _make_runner(adapter)
    original = service.runner.handle

    async def handler(event):
        key = "default-main"
        runner._session_state(key).turn.event = event
        if receipt_for(event) is not None:
            await complete_turn(service, key, key + "-sid", service.runner.home / "default",
                                "queue-root-0001", "completed")
        result = await runner._run_agent(
            message="native-origin" if receipt_for(event) is not None else event.text,
            context_prompt="", history=[], source=event.source,
            session_id=key + "-sid", session_key=key)
        return await runner._hmwa_deliver_turn_response(
            event, event.source, SimpleNamespace(session_id=key + "-sid"), key, None,
            result, [], result["final_response"], None, False)

    adapter._message_handler = handler

    async def admit(event):
        if DOCUMENT_TURN.get().phase != "origin":
            await original(event)
            return
        event._gateway_accepted = True
        receipts.append(receipt_for(event))
        notification = MessageEvent(text="background notification", source=event.source, internal=True)
        adapter._pending_messages["default-main"] = notification
        task = loop.create_task(adapter._process_message_background(event, "default-main"))
        adapter._session_tasks["default-main"] = task
        tasks.append(task)

    service.runner.adapter.handle_message = admit
    # Real queue depth, not the star fixture's zero-depth shortcut.
    service.runner._queue_depth = lambda key, **kwargs: runner._queue_depth(key, adapter=adapter)
    await send(star, "default-main", "peer", request="queue-root-0001", path=path)
    tick = loop.create_task(service.tick())
    try:
        await asyncio.wait_for(visible.wait(), timeout=3)
        # The platform has the final. A held unrelated model must not hold this receipt.
        final = await asyncio.wait_for(asyncio.shield(receipts[0].future), timeout=2)
        assert final == {"response": "native-final", "generated": True, "delivered": True,
                         "message_ids": ["native-msg"]}
        await asyncio.wait_for(tick, timeout=3)
        root = service.journal.get("queue-root-0001")
        assert root["status"] == "delivered" and root["continuation_confirmed"] is True
        assert root["return_message_ids"] == ["native-msg"]
        await asyncio.gather(*tasks)
        assert calls == ["native-origin"]
        assert len(drains) == 1 and not adapter._pending_messages
        release.set()
        event, key = drains[0]
        await adapter._process_message_background(event, key)
        assert calls == ["native-origin", "background notification"]
        assert [item["content"] for item in adapter.sent] == ["native-final", "notification-final"]
        assert receipts[0].future.result() == final
    finally:
        release.set()
        await asyncio.gather(tick, *tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_final_receipt_uses_own_interruption_not_unrelated_queue(monkeypatch, tmp_path, interrupted):
    service, _ = star = make_star(tmp_path, monkeypatch)
    original = service.runner.handle

    async def handle(event):
        capture_internal_delivery(event, SendResult(success=True, message_id=event.source.chat_id + "-final"))
        await original(event)
        if event.source.chat_id == "ops-main":
            service.runner.adapter._pending_messages["ops-main"] = MessageEvent(
                text="next notification", source=event.source, internal=True)
            if interrupted:
                invalidate_internal_receipt(event)

    service.runner.adapter.handle_message = handle
    service.runner._queue_depth = lambda key, **kwargs: int(key in service.runner.adapter._pending_messages)
    await send(star, "default-main", "peer", request="queue-root-0002")
    await service.tick()
    root = service.journal.get("queue-root-0002")
    if interrupted:
        assert root["status"] == "failed" and not root.get("continuation_confirmed")
        return
    assert root["status"] == "delivered"
    assert root["target_message_ids"] == ["ops-main-final"]
    assert root["return_message_ids"] == ["default-main-final"]
    assert service.runner.adapter._pending_messages["ops-main"].text == "next notification"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["human", "metadata", "wrong_type", "done", "interrupted", "steer", "other_key"])
async def test_native_deferral_cannot_swallow_normal_steered_or_forged_work(tmp_path, case):
    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    source = SessionSource(Platform.DISCORD, "owner", chat_type="group")
    event = MessageEvent(text="native work", source=source, internal=case != "human",
                         metadata={"request_id": "forged-root", "native_handoff_author": {"is_bot": True}})
    receipt = InternalTurnReceipt(asyncio.get_running_loop().create_future(), {"is_bot": True})
    if case not in {"human", "metadata"}:
        event._internal_turn_receipt = {"future": receipt.future} if case == "wrong_type" else receipt
    if case == "done":
        receipt.future.set_result({"generated": True})
    key = "default-main"
    runner._session_state(key + "-other" if case == "other_key" else key).turn.event = event
    pending = MessageEvent(text="queued", source=source, internal=True)
    adapter._pending_messages[key] = pending
    result = {"final_response": "final", "interrupted": case == "interrupted"}
    if case == "steer":
        result["pending_steer"] = "human instruction"
    next_event, text = await drain_pending_turn(runner, result, adapter, source, key)
    if case == "steer":
        assert next_event is None and text == "human instruction"
        assert adapter._pending_messages[key] is pending
    else:
        assert next_event is pending and text == "queued"
    if case in {"interrupted", "steer"}:
        assert receipt.interrupted is True and receipt.generated is False


@pytest.mark.asyncio
async def test_visible_bot_text_cannot_recover_an_ambiguous_root(monkeypatch, tmp_path):
    service, _ = star = make_star(tmp_path, monkeypatch)
    await send(star, "default-main", "peer", request="ambiguous-root-01")
    service.journal.update("ambiguous-root-01", status="ambiguous", outcome="completed",
                           error="native turn outcome unknown; do not resend")
    event = MessageEvent(text="root_request_id: ambiguous-root-01", internal=True,
                         source=service.runner.entries["default-main"].origin,
                         metadata={"request_id": "ambiguous-root-01", "message_id": "visible-bot-final"})
    capture_internal_response(event, event.text, successful=True)
    capture_internal_delivery(event, SendResult(success=True, message_id="visible-bot-final"))
    finish_internal_receipt(event, ProcessingOutcome.SUCCESS)
    assert receipt_for(event) is None
    await service.tick()
    assert service.journal.get("ambiguous-root-01")["status"] == "ambiguous"
    assert not service.runner.received and not service.runner.sent
