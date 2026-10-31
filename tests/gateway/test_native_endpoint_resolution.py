"""Endpoint adoption is a read-only, generation-fenced compression query."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.gateway import native_endpoint_fixtures as source_fixtures

shared_native = source_fixtures.shared_native
pytestmark = pytest.mark.platforms("posix")


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["none", "generation", "entry", "owner", "home", "publication", "agent", "reset", "fork", "empty"])
@pytest.mark.parametrize("boundary", ["lookup", "publication"])
async def test_effective_endpoint_rechecks_async_boundaries(shared_native, monkeypatch, race, boundary):
    from gateway import native_endpoint as native
    from gateway.session_identity import replace_source
    from hermes_state import SessionDB

    service, _, _, entry = shared_native
    runner = service.runner
    db = SessionDB(service.home / "state.db")
    parent, child = entry.session_id, entry.session_id + "-published"
    generation = runner._begin_session_run_generation(entry.session_key)
    agent = SimpleNamespace(session_id=parent, _session_db=db)
    ctx = SimpleNamespace(session_key=entry.session_key, session_id=parent, run_generation=generation)
    changed = False

    def mutate():
        nonlocal changed
        if changed:
            return
        changed = True
        mutations = {
            "none": lambda: None,
            "generation": lambda: runner._invalidate_session_run_generation(entry.session_key),
            "entry": lambda: runner.session_store._entries.update({entry.session_key: replace(entry, session_id="reset")}),
            "owner": lambda: setattr(entry, "origin", replace_source(entry.origin, user_id="colleague")),
            "home": lambda: setattr(agent, "_session_db", SimpleNamespace(db_path=service.home / "foreign" / "state.db")),
            "publication": lambda: db._write_sql("UPDATE sessions SET ended_at=1, end_reason='session_reset' WHERE id=?", (child,)),
            "agent": lambda: setattr(agent, "session_id", "unpublished-agent-id"),
            "reset": lambda: db._write_sql("UPDATE sessions SET end_reason='session_reset' WHERE id=?", (parent,)),
            "fork": lambda: db._write_sql("UPDATE sessions SET model_config=? WHERE id=?", ('{"_branched_from":"parent"}', child)),
            "empty": lambda: db._write_sql("UPDATE sessions SET message_count=0 WHERE id=?", (child,)),
        }
        mutations[race]()

    lookup = runner.async_session_store.lookup_by_session_key
    reads = 0

    async def raced_lookup(key):
        nonlocal reads
        found = await lookup(key)
        reads += 1
        if boundary == "lookup" and reads == 2:
            mutate()
        return found

    publish = native._published_endpoint

    def raced_publication(*args):
        sid = publish(*args)
        if boundary == "publication":
            # Mutations run on the owning gateway loop, while this DB read is off-loop.
            async def invalidate():
                mutate()
            asyncio.run_coroutine_threadsafe(invalidate(), runner._gateway_loop).result(5)
        return sid

    async def consume(*a, **kw):
        runner._verify_native_turn_agent(ctx, agent)
        db.publish_compression_child(parent_session_id=parent, child_session_id=child,
            source="discord", messages=[{"role": "user", "content": "published handoff"}],
            require_compression_lease=False)
        agent.session_id = child
        monkeypatch.setattr(runner.async_session_store, "lookup_by_session_key", raced_lookup)
        monkeypatch.setattr(native, "_published_endpoint", raced_publication)
        if race == "none":
            source, sid = await native.effective_endpoint(runner, entry.session_key)
            assert source.user_id == "owner" and sid == child
            assert entry.session_id == parent, "endpoint queries never synchronize entry"
        else:
            with pytest.raises(PermissionError, match="native"):
                await native.effective_endpoint(runner, entry.session_key)

    monkeypatch.setattr(runner, "_run_agent_inner", consume)
    try:
        await runner._run_agent("offline endpoint query", "", [], entry.origin, parent,
            session_key=entry.session_key, run_generation=generation)
        assert service.journal.pending() == []
        with service.journal.connect() as journal:
            assert journal.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()
