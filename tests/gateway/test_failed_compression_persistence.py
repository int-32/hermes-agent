"""Failed-turn publication must not overwrite a different key's committed route."""
import asyncio
import json
import threading

import pytest

from gateway.session_identity import replace_source
from tests.gateway import test_strict_compression_return as strict_fixtures

shared_native = strict_fixtures.shared_native
live_model = strict_fixtures.live_model
strict_intake = strict_fixtures.strict_intake
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


def _durable_routes(model):
    store = model.runner.session_store
    sql = {key: json.loads(value)["session_id"] for key, value in
           model.db.load_gateway_routing_entries(scope=store._routing_scope()).items()}
    mirror = {key: value["session_id"] for key, value in
              json.loads((store.sessions_dir / "sessions.json").read_text()).items() if key != "_README"}
    return {"sql": sql, "mirror": mirror}


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["reset", "create"])
async def test_failed_publication_orders_snapshot_revision_with_other_key(strict_intake, monkeypatch, transition):
    test = strict_intake
    model, adapter = test.model, test.adapter
    runner, store, service = model.runner, model.runner.session_store, model.service
    strict_fixtures.publication_fixtures.configure_request(model, "first")
    service.policy["execution_enabled"] = True
    source_b = replace_source(model.entry.origin, chat_id="independent-b", thread_id="independent-b")
    key_b = runner._session_key_for_source(source_b)
    assert key_b != model.entry.session_key
    old_b = None
    if transition == "reset":
        old_b = (await runner.async_session_store.get_or_create_session(source_b)).session_id
    captured, attempting, completed, armed = (threading.Event() for _ in range(4))
    trace, worker_errors = {}, []
    main_thread = threading.get_ident()
    snapshot = store._entries_as_dicts

    def pause_snapshot():
        data = snapshot()
        if armed.is_set() and threading.get_ident() == main_thread:
            armed.clear()
            trace["snapshot_b"] = data.get(key_b, {}).get("session_id")
            trace["snapshot_revision_before"] = store._routing_generation
            # A correct snapshot holds the entries lock through revision allocation.
            # Let B attempt its real transition in either case; only an unprotected
            # snapshot can admit B's completed save before A allocates its revision.
            unlocked = store._lock.acquire(blocking=False)
            if unlocked:
                store._lock.release()
            trace["snapshot_protected"] = not unlocked
            captured.set()
            assert attempting.wait(15), "B must attempt its routing transition"
            if unlocked:
                assert completed.wait(15), "unlocked A must admit B's complete durable save"
        return data

    def change_b():
        try:
            assert captured.wait(20), "A must capture its recovery snapshot"
            attempting.set()
            with runner._profile_scope_for_source(source_b):
                entry = store.reset_session(key_b) if transition == "reset" else store.get_or_create_session(source_b)
                trace["new_b"] = entry.session_id
                trace["b_committed_revision"] = store._persisted_routing_generation
                trace["b_committed"] = _durable_routes(model)
                assert all(routes[key_b] == entry.session_id for routes in trace["b_committed"].values())
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            completed.set()

    monkeypatch.setattr(store, "_entries_as_dicts", pause_snapshot)
    worker = threading.Thread(target=change_b, name="real-independent-route-transition")
    try:
        await adapter.handle_message(test.event)
        source_task = adapter._session_tasks[model.entry.session_key]
        await asyncio.wait_for(test.entered.wait(), 15)
        original = service.journal.get("publication-request")
        child = model.agents[0].session_id
        assert original["source_sid"] == child != model.entry.session_id
        generation = runner._current_session_run_generation(model.entry.session_key)
        worker.start()
        armed.set()
        test.release.set()
        await asyncio.wait_for(asyncio.shield(source_task), 30)
        assert await asyncio.to_thread(completed.wait, 20), "B must finish"
        await asyncio.to_thread(worker.join, 10)
        assert not worker.is_alive() and not worker_errors, worker_errors
        assert captured.is_set() and trace["snapshot_b"] == old_b
        assert trace["new_b"] != old_b
        assert runner._current_session_run_generation(model.entry.session_key) == generation
        assert model.entry.session_id == child and test.errors[0]["entry_before"] == child
        final = _durable_routes(model)
        trace.update(transition=transition, memory_b=store._entries[key_b].session_id,
                     final_revision=store._persisted_routing_generation, final=final, key_b=key_b,
                     generation=generation, source_child=child)
        print(json.dumps(trace, sort_keys=True))
        assert all(routes.get(key_b) == trace["memory_b"] == trace["new_b"] for routes in final.values()), trace
        # The original accepted R still traverses Base intake and the exact strict
        # resolver on both legs, after the competing structural save has finished.
        model.plan.fail = False
        await asyncio.wait_for(service.tick(), 30)
        settled = service.journal.get(original["request_id"])
        assert settled["status"] == "delivered" and settled["return_session_id"] == child, settled
        for name in ("request_id", "root_request_id", "source_sid", "source_key", "target_sid", "target_key", "document", "origin"):
            assert settled[name] == original[name]
        internal = [row for row in test.resolutions if row["internal"]]
        assert [row["phase"] for row in internal] == ["request", "origin"]
        assert all(row["strict"] and row["requested"] == row["resolved"] for row in internal)
        assert all(row["receipt"].future.result()["generated"] and row["receipt"].future.result()["delivered"] for row in internal)
        assert all(routes[key_b] == trace["new_b"] for routes in _durable_routes(model).values())
    finally:
        test.release.set()
        captured.set()
        if worker.ident is not None:
            await asyncio.to_thread(worker.join, 20)
        assert not worker.is_alive(), "fixture must not leave a persistence worker behind"
