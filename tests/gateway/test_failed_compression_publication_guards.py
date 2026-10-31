"""The failed-run publication fence cannot overwrite a replacement or unproven child."""
import asyncio
import json

import pytest

from tests.gateway import test_strict_compression_return as strict_fixtures

shared_native = strict_fixtures.shared_native
live_model = strict_fixtures.live_model
strict_intake = strict_fixtures.strict_intake
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


def damage_route(test, record, damage, monkeypatch):
    from gateway import native_endpoint
    model, runner = test.model, test.model.runner
    parent, child = model.entry.session_id, record["source_sid"]
    changes = {
        "reset": ("end_reason", "session_reset", parent),
        "fork": ("model_config", json.dumps({"_branched_from": parent}), child),
        "key": ("session_key", "foreign-key", child),
        "owner": ("user_id", "colleague", child),
        "profile": ("profile_name", "aiyu-ops", child),
        "ended": ("ended_at", 1, child),
    }
    if damage in changes:
        column, value, sid = changes[damage]
        model.db._write_sql(f"UPDATE sessions SET {column}=? WHERE id=?", (value, sid))
    if damage == "home":
        class ForeignDB:
            db_path = model.service.home / "foreign" / "state.db"

            def __getattr__(self, name):
                return getattr(model.db, name)

        model.agents[0]._session_db = ForeignDB()
    if damage == "generation":
        runner._invalidate_session_run_generation(record["source_key"])
    if damage == "entry":
        runner.session_store.get_or_create_session(test.event.source, force_new=True)
    if damage.startswith("late_"):
        publish = native_endpoint._publish_failed_compression

        def race_commit(*args):
            actions = {
                "late_generation": lambda: runner._invalidate_session_run_generation(record["source_key"]),
                "late_entry": lambda: runner.session_store.get_or_create_session(test.event.source, force_new=True),
                "late_reset": lambda: model.db._write_sql("UPDATE sessions SET end_reason='session_reset' WHERE id=?", (parent,)),
                "late_fork": lambda: model.db._write_sql("UPDATE sessions SET model_config=? WHERE id=?",
                                                        (json.dumps({"_branched_from": parent}), child)),
                "late_empty": lambda: model.db._write_sql("UPDATE sessions SET message_count=0 WHERE id=?", (child,)),
            }
            actions[damage]()
            return publish(*args)

        monkeypatch.setattr(native_endpoint, "_publish_failed_compression", race_commit)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["reset", "fork", "key", "owner", "profile", "home", "ended", "late_empty",
    "generation", "entry", "late_generation", "late_entry", "late_reset", "late_fork"])
async def test_failed_run_and_real_strict_intake_reject_invalid_publication(strict_intake, monkeypatch, damage):
    test = strict_intake
    model, adapter = test.model, test.adapter
    service, runner = model.service, model.runner
    strict_fixtures.publication_fixtures.configure_request(model, "first")
    old = model.entry.session_id
    try:
        await adapter.handle_message(test.event)
        assert test.event._gateway_accepted
        task = adapter._session_tasks[model.entry.session_key]
        await asyncio.wait_for(test.entered.wait(), 15)
        record = service.journal.get("publication-request")
        assert model.observed[0]["result"]["status"] == "queued"
        child = record["source_sid"]
        assert child != old == model.entry.session_id
        damage_route(test, record, damage, monkeypatch)
        test.release.set()
        await asyncio.wait_for(asyncio.shield(task), 15)
        current = await runner.async_session_store.lookup_by_session_key(record["source_key"])
        assert current.session_id != child, "failed worker must never publish an invalidated child"
        assert (current.session_id != old) == (damage in {"entry", "late_entry"})
        assert test.errors[0]["owned"]
        # A reset leaves the old parent's user tail open. Its ordinary failure
        # boundary may close that old turn, while the child must remain unpublished.
        appended = int(damage in {"reset", "late_reset"})
        assert test.errors[0]["after"] == test.errors[0]["before"] + appended
        # A stop/replacement releases its own running slot; the test's generation-only
        # mutation above deliberately did not fake a source identity or a new model.
        runner._release_running_agent_state(record["source_key"],
            run_generation=runner._current_session_run_generation(record["source_key"]))
        test.model.plan.fail = False
        outcome = await asyncio.wait_for(service.native_turn(record, runner._restored_source(current),
            "rejected strict continuation probe", record["source_key"], child, phase="origin"), 15)
        assert not outcome["generated"]
        last = test.resolutions[-1]
        assert last["internal"] and last["strict"] and last["requested"] == child and last["resolved"] is None
        assert len(model.observed) == 1, "strict rejection must happen before another model/tool worker"
        assert (await runner.async_session_store.lookup_by_session_key(record["source_key"])).session_id == current.session_id
    finally:
        test.release.set()
        for agent in model.agents:
            agent._session_db = model.db
