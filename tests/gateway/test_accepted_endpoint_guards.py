"""Accepted work is a bounded routing anchor, never authority for resets or forks."""
import asyncio
from contextlib import asynccontextmanager
import json
import threading

import pytest

from gateway.native_endpoint import NATIVE_TURN, effective_endpoint
from tests.gateway import test_queued_publication as publication_fixtures

shared_native = publication_fixtures.shared_native
live_model = publication_fixtures.live_model
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


@asynccontextmanager
async def accepted_window(model, lane):
    publication_fixtures.configure_request(model, lane)
    model.service.policy["execution_enabled"] = True
    arrived, release = asyncio.Event(), threading.Event()

    def hold():
        model.runner._gateway_loop.call_soon_threadsafe(arrived.set)
        assert release.wait(20)

    model.plan.after_tool = hold
    running = asyncio.create_task(model.run())
    try:
        await asyncio.wait_for(arrived.wait(), 10)
        assert model.observed[0]["result"]["status"] == "queued"
        record = model.service.journal.get("publication-request")
        assert record["source_sid"] == model.agents[0].session_id != model.entry.session_id
        yield record
    finally:
        release.set()
        await asyncio.wait_for(running, 10)


def corrupt_publication(model, record, damage, monkeypatch):
    from gateway import agent_handoff
    db, runner = model.db, model.runner
    old, child = model.entry.session_id, record["source_sid"]
    changes = {
        "reset": ("end_reason", "session_reset", old),
        "fork": ("model_config", json.dumps({"_branched_from": old}), child),
        "ended": ("ended_at", 1, child),
        "key": ("session_key", "foreign-key", child),
        "owner": ("user_id", "colleague", child),
        "profile": ("profile_name", "aiyu-ops", child),
        "origin": ("origin_json", "{}", child),
        "empty": ("message_count", 0, child),
    }
    if damage in changes:
        column, value, sid = changes[damage]
        db._write_sql(f"UPDATE sessions SET {column}=? WHERE id=?", (value, sid))
    actions = {
        "home": lambda: model.service.journal.update(record["request_id"], caller_home=str(model.service.home / "foreign")),
        "entry": lambda: setattr(model.entry, "session_id", "reset-entry"),
        "unpublished": lambda: model.service.journal.update(record["request_id"], source_sid="unpublished-cache-id"),
    }
    if damage in actions:
        actions[damage]()
    if damage == "generation":
        lookup = runner.async_session_store.lookup_by_session_key

        async def invalidate_during_lookup(key):
            found = await lookup(key)
            runner._invalidate_session_run_generation(key)
            return found

        monkeypatch.setattr(runner.async_session_store, "lookup_by_session_key", invalidate_during_lookup)
    if damage == "late_generation":
        continues = agent_handoff._continues

        def invalidate_after_endpoint(*args):
            async def invalidate():
                runner._invalidate_session_run_generation(record["source_key"])
            asyncio.run_coroutine_threadsafe(invalidate(), runner._gateway_loop).result(5)
            return continues(*args)

        monkeypatch.setattr(agent_handoff, "_continues", invalidate_after_endpoint)


NEGATIVES = [("native", damage) for damage in (
    "reset", "fork", "ended", "key", "owner", "profile", "origin", "empty", "home", "entry", "unpublished",
    "generation", "late_generation")]



@pytest.mark.asyncio
@pytest.mark.parametrize("lane,damage", NEGATIVES)
async def test_accepted_window_rejects_invalid_routes_before_dispatch(live_model, monkeypatch, lane, damage):
    model = live_model
    permit = asyncio.Event()

    async def independent_consumer():
        await permit.wait()
        assert NATIVE_TURN.get() is None
        await model.service.tick()

    # This consumer is born before the model's real native scope.
    consumer = asyncio.create_task(independent_consumer())
    events = []

    async def forbidden_delivery(event):
        events.append(event)
        raise AssertionError("invalid accepted route reached native admission")

    adapter = model.runner._delivery_adapter_for(model.entry.origin)
    monkeypatch.setattr(adapter, "handle_message", forbidden_delivery)
    try:
        async with accepted_window(model, lane) as record:
            # Ordinary off-turn reads still return the approved entry, even though the
            # accepted consumer can resolve its forward publication through the DB.
            _, ordinary = await effective_endpoint(model.runner, record["source_key"])
            assert ordinary == model.entry.session_id != record["source_sid"]
            corrupt_publication(model, record, damage, monkeypatch)
            permit.set()
            await asyncio.wait_for(consumer, 10)
            current = model.service.journal.get(record["request_id"])
            assert current["status"] == "failed" and current["error"] == "PermissionError", current
            assert not events
        await model.service.tick()
        assert not events, "unsafe failed work requires explicit recovery, never automatic replay"
    finally:
        consumer.cancel()
