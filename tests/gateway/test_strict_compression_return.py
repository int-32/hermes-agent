"""Failed native workers must publish compression before real strict receipt intake."""
import asyncio
from contextlib import suppress
from functools import wraps
import json
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio

from gateway.coordination_receipts import DOCUMENT_TURN
from gateway.internal_receipt import receipt_for
from gateway.native_endpoint import NATIVE_TURN
from gateway.platforms.event import MessageEvent
from tests.gateway import test_queued_publication as publication_fixtures

shared_native = publication_fixtures.shared_native
live_model = publication_fixtures.live_model
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


@pytest_asyncio.fixture
async def strict_intake(live_model, monkeypatch):
    from agent import conversation_loop

    model = live_model
    runner = model.runner
    defaults = object.__new__(type(runner))
    defaults.config = runner.config
    defaults._init_lifecycle_state()
    defaults._init_runtime_caches()
    defaults._init_registries_and_clocks()
    for name, value in defaults.__dict__.items():
        runner.__dict__.setdefault(name, value)
    runner.hooks = defaults.hooks
    runner._busy_text_mode = "queue"
    adapter = runner._delivery_adapter_for(model.entry.origin)
    adapter.config.typing_indicator = False
    runner._wire_adapter_handlers(adapter)
    entered, release = asyncio.Event(), threading.Event()
    errors, resolutions, turns = [], [], []
    model.plan.fail = True

    def hold():
        runner._gateway_loop.call_soon_threadsafe(entered.set)
        assert release.wait(30), "test must release the source model"

    model.plan.after_tool = hold
    finalize = conversation_loop.finalize_turn

    @wraps(finalize)
    def fail_source_finalization(*args, **kwargs):
        if model.plan.fail and DOCUMENT_TURN.get() is None:
            raise RuntimeError("scripted finalization failure after accepted tool call")
        return finalize(*args, **kwargs)

    monkeypatch.setattr(conversation_loop, "finalize_turn", fail_source_finalization)
    error_reply = runner._hmwa_agent_error_reply

    async def observe_error(exc, event, source, entry, key, prepared):
        owned = await runner.async_session_store.has_input_owner(prepared.persistence_session_id, prepared.persistence_owner)
        tail = await runner.async_session_store.transcript_tail_role(entry.session_id)
        entry_before = entry.session_id
        before = model.db._read_one("SELECT COUNT(*) AS n FROM messages")["n"]
        reply = await error_reply(exc, event, source, entry, key, prepared)
        errors.append({"owned": owned, "tail": tail, "entry_before": entry_before, "entry": entry.session_id,
                       "before": before, "after": model.db._read_one("SELECT COUNT(*) AS n FROM messages")["n"]})
        return reply

    monkeypatch.setattr(runner, "_hmwa_agent_error_reply", observe_error)
    resolve = runner._hmwa_resolve_session

    async def observe_resolver(event, source):
        found = await resolve(event, source)
        resolutions.append({"internal": event.internal, "key": (event.metadata or {}).get("gateway_session_key"),
            "requested": (event.metadata or {}).get("gateway_session_id"),
            "strict": (event.metadata or {}).get("gateway_session_strict"),
            "resolved": found[1].session_id if found else None, "phase": DOCUMENT_TURN.get().phase if DOCUMENT_TURN.get() else None,
            "receipt": receipt_for(event)})
        return found

    monkeypatch.setattr(runner, "_hmwa_resolve_session", observe_resolver)
    handle = runner._handle_message_with_agent

    async def observe_turn(event, source, key, generation):
        turn = DOCUMENT_TURN.get()
        turns.append((key, turn.phase if turn else "human"))
        if turn:
            model.plan.calls, model.plan.transition, model.plan.after_tool = 0, "none", None
            model.plan.keys.add(key)
            model.plan.args = {"action": "complete", "request_id": turn.work["request_id"], "outcome": "completed"}
        return await handle(event, source, key, generation)

    monkeypatch.setattr(runner, "_handle_message_with_agent", observe_turn)
    owner = model.entry.origin
    source = adapter.build_source(chat_id=owner.chat_id, chat_type=owner.chat_type, user_id=owner.user_id,
        thread_id=owner.thread_id, parent_chat_id=owner.parent_chat_id, scope_id=owner.scope_id)
    event = MessageEvent(text="do the offline native task " + "prior context " * 300,
                         source=source, message_id="strict-source-input", allow_gateway_control=False)
    yield SimpleNamespace(model=model, adapter=adapter, event=event, entered=entered, release=release,
                          errors=errors, resolutions=resolutions, turns=turns)
    release.set()
    await asyncio.gather(*list(adapter._session_tasks.values()), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["native"])
@pytest.mark.parametrize("rebuild", [False, True], ids=["model-error", "service-rebuild"])
async def test_original_request_returns_through_real_strict_intake(strict_intake, monkeypatch, lane, rebuild):
    from gateway.agent_handoff import HandoffService, is_engaged

    test = strict_intake
    model, adapter = test.model, test.adapter
    runner, service = model.runner, model.service
    publication_fixtures.configure_request(model, lane)
    service.policy["execution_enabled"] = True
    assert not is_engaged() and not runner._draining and not runner._external_drain_active
    old = model.entry.session_id
    permits = asyncio.Queue()
    tick = service.tick

    async def gated_tick():
        done = await permits.get()
        assert NATIVE_TURN.get() is None and DOCUMENT_TURN.get() is None
        try:
            await tick()
        finally:
            done.set_result(None)

    monkeypatch.setattr(service, "tick", gated_tick)
    watcher = asyncio.create_task(service.watch())
    try:
        await adapter.handle_message(test.event)
        assert test.event._gateway_accepted
        source_task = adapter._session_tasks[model.entry.session_key]
        await asyncio.wait_for(test.entered.wait(), 15)
        assert model.observed[0]["result"]["status"] == "queued", model.observed
        original = service.journal.get("publication-request")
        child = model.agents[0].session_id
        assert child == original["source_sid"] != old == model.entry.session_id
        test.release.set()
        await asyncio.wait_for(asyncio.shield(source_task), 15)
        assert len(test.errors) == 1 and test.errors[0]["owned"]
        assert test.errors[0]["tail"] in {"assistant", "tool"}, test.errors
        assert test.errors[0]["before"] == test.errors[0]["after"], "error cleanup must not heal via a transcript append"
        current = await runner.async_session_store.lookup_by_session_key(original["source_key"])
        assert current.session_id == child and test.errors[0]["entry_before"] == test.errors[0]["entry"] == child
        model.plan.fail = False
        if rebuild:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            service = HandoffService(runner, service.home, service.policy)
            tick = service.tick
            monkeypatch.setattr(service, "tick", gated_tick)
            monkeypatch.setattr("gateway.agent_handoff._service", service)
            model.plan.service = service
            watcher = asyncio.create_task(service.watch())
        done = asyncio.get_running_loop().create_future()
        await permits.put(done)
        await asyncio.wait_for(done, 30)
        settled = service.journal.get(original["request_id"])
        print(json.dumps({"lane": lane, "rebuild": rebuild, "old": old, "child": child,
            "error_cleanup": test.errors, "resolvers": [{k: v for k, v in row.items() if k != "receipt"} for row in test.resolutions],
            "status": settled["status"]}))
        assert settled["status"] == "delivered", settled
        assert settled["continuation_confirmed"] and settled["return_session_id"] == child
        for name in ("request_id", "root_request_id", "source_sid", "source_key", "target_sid", "target_key", "document", "origin"):
            assert settled[name] == original[name]
        internal = [row for row in test.resolutions if row["internal"]]
        assert [row["phase"] for row in internal] == ["request", "origin"]
        assert all(row["strict"] and row["requested"] == row["resolved"] for row in internal)
        assert all(row["receipt"].future.result()["generated"] and row["receipt"].future.result()["delivered"] for row in internal)
        assert [ack["phase"] for ack in settled["acknowledgements"]] == ["request", "origin"]
    finally:
        test.release.set()
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
