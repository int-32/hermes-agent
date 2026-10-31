"""Rejected native wakes must not strand human FIFO behind the adapter slot."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.internal_receipt import InternalTurnReceipt
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from tests.gateway.test_queued_followup_processing_hooks import _install_fake_agent
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    calls, admitted, rejected = [], [], []
    sending, release_send = asyncio.Event(), asyncio.Event()
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
            if content == "answer-N":
                sending.set()
                await release_send.wait()
            await super().send(chat_id, content, reply_to, metadata)
            return SendResult(success=True, message_id="message-" + str(len(self.sent)))

    _install_fake_agent(monkeypatch, tmp_path, Agent)
    adapter = Adapter(platform=Platform.DISCORD)
    adapter.config.typing_indicator = False
    runner = _make_runner(adapter)
    adapter.gateway_runner = runner
    runner._update_runtime_status = lambda *args, **kwargs: None
    runner._session_key_for_source = lambda source: key

    def lookup(session_key):
        assert session_key == key
        rejected.append("lookup")
        if rejection == "exception":
            raise RuntimeError("offline session lookup failed")
        return SimpleNamespace(session_id="current-sid")

    runner.session_store.lookup_by_session_key = lookup
    rejection = None

    async def handler(event):
        admitted.append(event.text)
        if event.text == "W":
            # Real admission and strict routing/SID guards, before any model execution.
            result = await runner._handle_message_with_agent(event, event.source, key, 1)
            rejected.append("returned")
            return result
        runner._session_state(key).turn.event = event
        result = await runner._run_agent(message=event.text, context_prompt="", history=[],
                                        source=source, session_id="native-queue-sid", session_key=key)
        return await runner._hmwa_deliver_turn_response(
            event, source, SimpleNamespace(session_id="native-queue-sid"), key, None,
            result, [], result["final_response"], None, False)

    adapter._message_handler = handler

    async def run(*, arrival="existing", reject="sid"):
        nonlocal rejection
        rejection = reject
        first = MessageEvent(text="N", source=source, internal=True)
        first._internal_turn_receipt = InternalTurnReceipt(
            asyncio.get_running_loop().create_future(), {"id": "bot:peer", "is_bot": True})
        wake = MessageEvent(text="W", source=source, internal=True, metadata={
            "gateway_session_key": key if reject != "route" else "foreign-route",
            "gateway_session_id": "expired-sid", "gateway_session_strict": True})
        user = MessageEvent(text="U2", source=source)
        runner._queue_or_replace_pending_event(key, wake)
        if arrival == "existing":
            runner._queue_or_replace_pending_event(key, user)
        assert adapter._start_session_processing(first, key)
        try:
            if arrival == "final-send":
                await asyncio.wait_for(sending.wait(), timeout=5)
                # Native busy intake while the actual final transport await is suspended.
                runner._queue_or_replace_pending_event(key, user)
                assert list(runner._overflow_queue(key)) == [user]
            release_send.set()
            # Follow actual owned tasks, including every fresh native drain they spawn.
            for _ in range(8):
                tasks = list(adapter._background_tasks)
                if not tasks:
                    break
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert not adapter._background_tasks
        finally:
            release_send.set()
            await adapter.cancel_background_tasks()
        return SimpleNamespace(calls=calls, admitted=admitted, rejected=rejected,
                               adapter=adapter, runner=runner, key=key, receipt=first._internal_turn_receipt,
                               overflow=list(runner._overflow_queue(key) or ()))

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["existing", "final-send"])
@pytest.mark.parametrize("reject", ["sid", "route", "exception"])
async def test_rejected_wake_drains_human_overflow_without_new_input(pipeline, arrival, reject):
    result = await pipeline(arrival=arrival, reject=reject)
    assert result.calls == ["N", "U2"]
    assert result.admitted == ["N", "W", "U2"]
    assert result.rejected == (["lookup"] if reject == "exception" else
                               ["returned"] if reject == "route" else ["lookup", "returned"])
    assert [x["content"] for x in result.adapter.sent if x["content"].startswith("answer-")] == [
        "answer-N", "answer-U2"]
    assert not result.overflow
    assert not result.adapter._pending_messages
    assert not result.adapter._session_tasks
    assert not result.adapter._active_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["successor", "guard", "unowned", "no-guard", "draining", "human", "slot"])
async def test_cleanup_cannot_steal_another_owner_or_reorder_a_slot(boundary):
    from gateway.internal_receipt import promote_owned_internal_overflow

    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    adapter.gateway_runner = runner
    key = "agent:main:discord:group:owned"
    source = SessionSource(Platform.DISCORD, "owned", chat_type="group")
    event = MessageEvent(text="W", source=source, internal=boundary != "human")
    human = MessageEvent(text="U2", source=source)
    guard = asyncio.Event()
    runner._session_state(key).conversation.queued_events.append(human)
    successor = asyncio.get_running_loop().create_future()
    owner = {"successor": successor, "unowned": None}.get(boundary, asyncio.current_task())
    active_guard = {"guard": asyncio.Event(), "no-guard": None}.get(boundary, guard)
    if owner is not None:
        adapter._session_tasks[key] = owner
    if active_guard is not None:
        adapter._active_sessions[key] = active_guard
    runner._draining = boundary == "draining"
    if boundary == "slot":
        adapter._pending_messages[key] = event
    before = dict(adapter._pending_messages)
    promote_owned_internal_overflow(adapter, event, key, guard if boundary != "no-guard" else None)
    assert list(runner._overflow_queue(key)) == [human]
    assert adapter._pending_messages == before
    assert not adapter._background_tasks
    successor.cancel()
