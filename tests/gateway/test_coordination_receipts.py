"""A development dependency resumes through both mains before its origin can finish."""
import json

from tests.gateway.test_agent_handoff import native_owner

import pytest

from gateway.agent_handoff import HandoffService
from gateway.internal_receipt import capture_internal_response, finish_internal_receipt
from gateway.platforms.event import ProcessingOutcome
from tests.gateway.test_agent_coordination import make_star, send
from tests.gateway.test_agent_handoff import Runner

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def star(tmp_path, monkeypatch):
    return make_star(tmp_path, monkeypatch)

@pytest.mark.asyncio
async def test_asset_dependency_is_not_a_final_result_and_resumes_original_task(star):
    service, _ = star
    service.runner.adapter.handle_message = lambda event: Runner.handle(service.runner, event)
    phases = []

    async def handle(event):
        phases.append(event.source.chat_id)
        event._gateway_accepted = True
        if len(phases) == 1:
            await send(star, "default-main", "peer", request="asset-child-01")
            response = "queued; awaiting independent ops"
        else:
            from gateway.coordination_receipts import complete_turn
            work = "asset-child-01" if event.source.chat_id == "ops-main" else "request-0001"
            await complete_turn(service, event.source.chat_id, event.source.chat_id + "-sid",
                                service.runner.home / event.source.profile, work, "completed")
            response = "verified asset nonce; continued original development"
        capture_internal_response(event, response, successful=True)
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)

    service.runner.adapter.handle_message = handle
    await send(star, "default-task", "main")
    await service.tick()
    assert service.journal.get("request-0001")["status"] == "waiting_children"
    assert not service.runner.sent
    # Persisted dependency survives an idle gateway restart, not a process-local callback.
    restarted = HandoffService(service.runner, service.home, service.policy)
    await restarted.tick()
    assert phases == ["default-main", "ops-main", "default-main"]
    assert restarted.journal.get("request-0001")["status"] == "generated"
    await restarted.tick()
    assert phases[-1] == "default-task"
    root = restarted.journal.get("request-0001")
    assert root["status"] == "delivered" and root["outcome"] == "completed"
    assert root["continuation_confirmed"] is True
    child = restarted.journal.get("asset-child-01")
    assert child["parent_request_id"] == "request-0001" and child["root_request_id"] == "request-0001"
    await restarted.tick()
    assert len(phases) == 4 and not service.runner.sent


@pytest.mark.asyncio
async def test_ack_without_explicit_completion_is_blocked_not_business_success(star):
    service, _ = star
    service.runner.adapter.handle_message = lambda event: Runner.handle(service.runner, event)
    await send(star, "default-task", "main")
    await service.tick()
    record = service.journal.get("request-0001")
    assert record["status"] == "blocked"
    assert record["outcome"] == "blocked" and "completion" in record["error"]
    assert len(service.runner.received) == 2
    assert not service.runner.sent


@pytest.mark.asyncio
async def test_busy_child_and_parent_completion_cannot_finish_early(star):
    from gateway.coordination_receipts import complete_turn
    service, _ = star
    original = service.runner.handle
    waiting = set()
    service.runner._is_session_running = lambda key: key in waiting

    async def receive(event):
        if event.source.chat_id == "default-main" and len(service.runner.received) == 0:
            await send(star, "default-main", "peer", request="asset-child-02")
            with pytest.raises(PermissionError, match="pending"):
                await complete_turn(service, "default-main", "default-main-sid",
                                    service.runner.home / "default", "request-0001", "completed")
            waiting.add("ops-main")
            await Runner.handle(service.runner, event)
        else:
            await original(event)

    service.runner.adapter.handle_message = receive
    await send(star, "default-task", "main")
    await service.tick(); await service.tick()
    assert service.journal.get("request-0001")["status"] == "waiting_children"
    assert service.journal.get("asset-child-02")["status"] == "queued"
    assert [e.source.chat_id for e in service.runner.received] == ["default-main"]
    waiting.clear()
    await service.tick(); await service.tick()
    assert service.journal.get("request-0001")["status"] == "delivered"
    assert [e.source.chat_id for e in service.runner.received] == [
        "default-main", "ops-main", "default-main", "default-task"]


@pytest.mark.asyncio
async def test_generated_receipt_waits_for_source_and_survives_restart_once(star):
    service, _ = star
    original = service.runner.handle
    blocked = set()
    service.runner._is_session_running = lambda key: key in blocked

    async def receive(event):
        await original(event)
        if event.source.chat_id == "default-main":
            blocked.add("default-task")

    service.runner.adapter.handle_message = receive
    await send(star, "default-task", "main")
    await service.tick()
    assert service.journal.get("request-0001")["status"] == "generated"
    assert len(service.runner.received) == 1
    restarted = HandoffService(service.runner, service.home, service.policy)
    await restarted.tick()
    assert len(service.runner.received) == 1
    blocked.clear()
    await restarted.tick(); await restarted.tick()
    assert len(service.runner.received) == 2 and not service.runner.sent
    await send((restarted, star[1]), "default-task", "main")
    await restarted.tick()
    assert len(service.runner.received) == 2


@pytest.mark.asyncio
async def test_native_completion_cannot_be_forged_outside_turn_or_for_other_origin(star):
    from gateway.coordination_receipts import complete_turn
    service, _ = star
    with pytest.raises(PermissionError, match="private native"):
        await complete_turn(service, "default-main", "default-main-sid", service.runner.home / "default",
                            "request-0001", "completed")
    original = service.runner.handle

    async def receive(event):
        for key, sid, home, request in [
            ("ops-main", "ops-main-sid", "aiyu-ops", "request-0001"),
            (event.source.chat_id, event.source.chat_id + "-sid", event.source.profile, "forged-00001"),
            (event.source.chat_id, "reset", event.source.profile, "request-0001"),
            (event.source.chat_id, event.source.chat_id + "-sid", "other", "request-0001")]:
            with pytest.raises(PermissionError):
                await complete_turn(service, key, sid, service.runner.home / home, request, "completed")
        await original(event)

    service.runner.adapter.handle_message = receive
    await send(star, "default-task", "main")
    await service.tick()
    assert service.journal.get("request-0001")["status"] == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["blocked", "interrupted", "refused"])
async def test_child_failure_or_interruption_never_finishes_parent(star, failure):
    from gateway.coordination_receipts import DOCUMENT_TURN, complete_turn
    from gateway.internal_receipt import invalidate_internal_receipt
    service, _ = star
    original = service.runner.handle
    stages = []

    async def receive(event):
        turn = DOCUMENT_TURN.get()
        stages.append(event.source.chat_id)
        if len(stages) == 1:
            await send(star, "default-main", "peer", request="asset-failure-01")
            await Runner.handle(service.runner, event)
        elif event.source.chat_id == "ops-main" and failure == "blocked":
            await complete_turn(service, "ops-main", "ops-main-sid", service.runner.home / "aiyu-ops",
                                "asset-failure-01", "blocked")
            await Runner.handle(service.runner, event)
        elif event.source.chat_id == "ops-main" and failure == "refused":
            return
        elif turn.phase == "continuation":
            if failure == "interrupted":
                invalidate_internal_receipt(event)
            else:
                with pytest.raises(PermissionError, match="blocked dependency"):
                    await complete_turn(service, "default-main", "default-main-sid",
                                        service.runner.home / "default", "request-0001", "completed")
                await complete_turn(service, "default-main", "default-main-sid",
                                    service.runner.home / "default", "request-0001", "blocked")
            await Runner.handle(service.runner, event)
        else:
            await original(event)

    service.runner.adapter.handle_message = receive
    await send(star, "default-task", "main")
    for _ in range(4):
        await service.tick()
    record = service.journal.get("request-0001")
    assert record["status"] == "blocked"
    assert record["outcome"] == "blocked"
    assert not service.runner.sent
    previous = len(stages)
    await HandoffService(service.runner, service.home, service.policy).tick()
    assert len(stages) == previous


@pytest.mark.asyncio
async def test_uncertain_return_is_not_replayed_or_joined_after_restart(star):
    service, _ = star
    await send(star, "default-task", "main")
    service.journal.update("request-0001", status="returning", outcome="completed", response="native result")
    restarted = HandoffService(service.runner, service.home, service.policy)
    await restarted.tick()
    assert restarted.journal.get("request-0001")["status"] == "ambiguous"
    assert not service.runner.received and not service.runner.sent


@pytest.mark.asyncio
async def test_native_final_message_ids_are_captured_without_forged_metadata(star):
    from gateway.internal_receipt import capture_internal_delivery, receipt_for
    service, _ = star
    original = service.runner.handle

    async def receive(event):
        assert receipt_for(event) is not None
        capture_internal_delivery(event, type("R", (), {"success": True, "message_id": event.source.chat_id + "-msg"})())
        await original(event)

    service.runner.adapter.handle_message = receive
    await send(star, "default-task", "main")
    await service.tick()
    record = service.journal.get("request-0001")
    assert record["target_message_ids"] == ["default-main-msg"]
    assert record["return_message_ids"] == ["default-task-msg"]


@pytest.mark.asyncio
async def test_real_final_send_records_native_receipt_message_id(star, monkeypatch):
    from unittest.mock import AsyncMock
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.internal_receipt import InternalTurnReceipt
    from gateway.platforms.event import MessageEvent
    import asyncio
    from types import SimpleNamespace
    service, _ = star
    adapter = SimpleNamespace(name="discord", _send_with_retry=AsyncMock(
        return_value=SendResult(success=True, message_id="actual-final-message")),
        _record_delivery_obligation=AsyncMock(return_value=None))
    adapter._final_delivery_adapter = lambda source: adapter
    event = MessageEvent(text="receipt", internal=True, source=service.runner.entries["default-task"].origin)
    event._internal_turn_receipt = InternalTurnReceipt(asyncio.get_running_loop().create_future(), {"is_bot": True})
    monkeypatch.setattr("gateway.platforms.base.stop_reply_clock", lambda *a: None)
    await BasePlatformAdapter.send_final_ledgered(adapter, event, "default-task", "actual final", {}, reply_to=None)
    assert event._internal_turn_receipt.message_ids == ["actual-final-message"]
    assert adapter._send_with_retry.await_count == 1


def test_dependency_join_updates_are_atomic(star):
    service, _ = star
    journal = service.journal
    journal.enqueue({"request_id": "atomic-00001", "status": "queued"})
    journal.enqueue({"request_id": "atomic-00002", "status": "queued"})
    # A failed second write cannot leave the parent eligible to finish alone.
    with pytest.raises(TypeError):
        journal.update_many({"atomic-00001": {"status": "generated"},
                             "atomic-00002": {"unserializable": object()}})
    assert journal.get("atomic-00001")["status"] == "queued"
    assert journal.get("atomic-00002")["status"] == "queued"


@pytest.mark.asyncio
async def test_controller_ack_only_after_child_receipt_cannot_complete_work(star):
    from gateway.coordination_receipts import DOCUMENT_TURN
    service, _ = star
    original = service.runner.handle

    async def receive(event):
        turn = DOCUMENT_TURN.get()
        if turn.phase == "request" and event.source.chat_id == "default-main":
            await send(star, "default-main", "peer", request="ack-child-0001")
            await Runner.handle(service.runner, event)
        elif turn.phase == "continuation":
            # No new dependency and no completion: 'received/forwarded' cannot finish the parent.
            await Runner.handle(service.runner, event)
        else:
            await original(event)

    service.runner.adapter.handle_message = receive
    await send(star, "default-task", "main")
    await service.tick(); await service.tick(); await service.tick()
    assert service.journal.get("request-0001")["status"] == "blocked"
    child = service.journal.get("ack-child-0001")
    assert child["status"] == "blocked" and child["continuation_outcome"] == "blocked"
    assert not service.runner.sent
