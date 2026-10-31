"""Native FIFO cache reuse must continue a real compression lineage."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from collections import OrderedDict

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session_identity import replace_source
from tests.gateway import native_endpoint_fixtures as review_regressions
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]
shared_native = review_regressions.shared_native


def _response(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls" if tool_calls else "stop",
        message=SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None, reasoning=None))],
        usage=None, model="offline-script")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["request"])
@pytest.mark.parametrize("transition", ["compression", "reset", "fork", "generation"])
async def test_fifo_cached_agent_uses_verified_compression_sid(shared_native, monkeypatch, action, transition):
    import gateway.run as gateway_run
    import model_tools
    import run_agent
    from gateway.session_context import _SESSION_NATIVE_SOURCE, _SESSION_ID, _SESSION_KEY
    from tools.registry import registry
    from run_agent import AIAgent

    service, path, _, entry = shared_native
    runner = service.runner
    from hermes_state import SessionDB, AsyncSessionDB
    db = SessionDB(service.home / "state.db")
    old_sid = entry.session_id
    if db.get_session(old_sid) is None:
        db.create_session(old_sid, source="discord")
    runner._session_db = AsyncSessionDB(db)
    runner._agent_cache, runner._agent_cache_lock = OrderedDict(), threading.Lock()
    rotations = []
    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    adapter.gateway_runner = runner
    for name, value in _make_runner(adapter).__dict__.items():
        runner.__dict__.setdefault(name, value)
    old_adapter = runner.adapters[Platform.DISCORD]
    runner.adapters = {Platform.DISCORD: adapter}
    runner._draining = False
    runner._service_tier = None
    runner._admit_bot_message_for_source = lambda source: True
    runner._update_runtime_status = lambda *a, **kw: None
    runner._persist_active_agents = lambda *a: None
    runner._run_post_turn_hooks = AsyncMock()
    runner.hooks.emit = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    runner._hmwa_resolve_session = AsyncMock()
    runner._hmwa_open_session = AsyncMock(return_value=(False, False))
    runner._hmwa_acquire_turn_lease = AsyncMock()
    runner._mark_durable_active_turn = AsyncMock()
    runner._hmwa_run_session_hygiene = AsyncMock(return_value=[])
    runner._hmwa_first_contact_notes = AsyncMock()
    runner._pinned_session_context_prompt = lambda *a, **kw: ""
    runner._resolve_session_agent_runtime = lambda **kw: ("offline-script", {
        "api_key": "offline", "provider": "openai", "api_mode": "chat_completions",
        "base_url": "http://127.0.0.1:1/v1"})
    runner._hmwa_agent_error_reply = AsyncMock(side_effect=lambda error, *a: (_ for _ in ()).throw(error))
    started, release = threading.Event(), threading.Event()
    observations, model_turns, agents = [], [], []
    args = {"action": action, "request_id": "queued-native-request", "task_id": "task-one",
            "handoff_path": str(path), "target": "main"}

    class ScriptedAgent(AIAgent):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "skip_memory": True, "skip_context_files": True,
                                "load_soul_identity": False, "save_trajectories": False})
            self.compression_enabled = False
            self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.script)))
            self.compression_in_place = False
            compressor = MagicMock()
            compressor.compress.return_value = [
                {"role": "user", "content": "[CONTEXT COMPACTION] scripted summary"},
                {"role": "assistant", "content": "opening finished"}]
            compressor.compression_count = 1
            compressor.last_prompt_tokens = compressor.last_completion_tokens = 0
            compressor._last_summary_error = compressor._last_aux_model_failure_model = None
            compressor._last_aux_model_failure_error = None
            compressor._last_compress_aborted = compressor._last_summary_fallback_used = False
            compressor._last_compression_made_progress = True
            self.context_compressor = compressor
            self._compression_feasibility_checked = True
            agents.append(self)

        def run_conversation(self, user_message, **kwargs):
            result = super().run_conversation(user_message, **kwargs)
            if "OPENING" in str(user_message):
                if transition in {"compression", "generation"}:
                    compressed, _ = self._compress_context(result["messages"], self._cached_system_prompt,
                        approx_tokens=120_000, force=True)
                else:
                    from agent.conversation_compression import _rebind_session_context
                    child = old_sid + "-" + transition
                    db.end_session(old_sid, "session_reset" if transition == "reset" else "compression")
                    db.create_session(child, source="discord", parent_session_id=old_sid,
                        model_config={"_branched_from": old_sid} if transition == "fork" else {})
                    self.session_id = child
                    _rebind_session_context(child)
                    compressed = result["messages"]
                rotations.append((old_sid, self.session_id, _SESSION_ID.get()))
                assert self.session_id != old_sid, "scripted real compression must publish a child"
                result.update(messages=compressed, session_id=self.session_id)
                if transition == "generation":
                    async def invalidate():
                        runner._invalidate_session_run_generation(entry.session_key)
                    asyncio.run_coroutine_threadsafe(invalidate(), runner._gateway_loop).result(5)
            return result

        def _create_request_openai_client(self, **kwargs):
            return self.client

        def script(self, **kwargs):
            messages = kwargs["messages"]
            user = next(str(m["content"]) for m in reversed(messages) if m["role"] == "user")
            model_turns.append((id(self), self.session_id, _SESSION_ID.get(), threading.current_thread()))
            if "OPENING" in user and "FOLLOWUP" not in user:
                started.set()
                assert release.wait(10), "test must release the opening model"
                return _response("opening finished")
            if not any(m["role"] == "tool" for m in messages):
                call = SimpleNamespace(id="queued-call", type="function", function=SimpleNamespace(
                    name="coordinate_session", arguments=json.dumps(args)))
                return _response(tool_calls=[call])
            return _response("followup finished")

    monkeypatch.setattr(run_agent, "AIAgent", ScriptedAgent)
    monkeypatch.setattr(gateway_run, "_hermes_home", service.home)
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **kw: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"model": "offline-script", "display": {"tool_progress": "off"}})
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *a: {"agent_handoff"})
    schema = {"type": "function", "function": registry.get_entry("coordinate_session").schema}
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [schema])
    original_dispatch = model_tools.handle_function_call

    def observe_dispatch(*a, **kw):
        result = original_dispatch(*a, **kw)
        observations.append({"result": json.loads(result), "source": _SESSION_NATIVE_SOURCE.get().user_id,
                             "key": _SESSION_KEY.get(), "sid": _SESSION_ID.get(), "passed_sid": kw.get("session_id"), "thread": threading.current_thread()})
        return result

    monkeypatch.setattr(model_tools, "handle_function_call", observe_dispatch)
    initial = replace_source(entry.origin, user_id="owner")
    follower = replace_source(entry.origin, user_id="owner")
    runner._hmwa_resolve_session.return_value = initial, entry, entry.session_key
    event = MessageEvent(text="OPENING", source=initial, message_id="opening")
    command = MessageEvent(text="/queue FOLLOWUP", source=follower, message_id="queued")
    generation = runner._begin_session_run_generation(entry.session_key)
    task = asyncio.create_task(runner._handle_message_with_agent(event, initial, entry.session_key, generation))
    try:
        assert await asyncio.to_thread(started.wait, 10), "opening model never ran"
        assert runner._is_user_authorized_for_source(follower)
        await runner._busy_queue_command(command, entry.session_key, follower)
        pending = adapter._pending_messages[entry.session_key]
        assert pending.source.user_id == "owner" and runner._queue_depth(entry.session_key, adapter=adapter) == 1
        release.set()
        if transition == "compression":
            await asyncio.wait_for(task, 30)
            assert db.get_compression_tip(old_sid) == entry.session_id
        else:
            with pytest.raises(PermissionError, match="queued"):
                await asyncio.wait_for(task, 30)
            assert not observations
            with service.journal.connect() as journal_db:
                assert journal_db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for agent in agents:
            agent.close()
        db.close()
        assert old_adapter is not None  # retain the original real intake provenance until cleanup
    if transition != "compression":
        assert len(agents) == 1 and len(model_turns) == 1
        return
    assert observations and len(model_turns) >= 3
    assert len(agents) == 1, "the queued turn must really reuse the cached AIAgent"
    child = rotations[0][1]
    assert rotations == [(old_sid, child, child)]
    assert all(item[0] == id(agents[0]) for item in model_turns)
    assert model_turns[1][1] == child and observations[0]["passed_sid"] == child
    assert entry.session_id == child
    result = observations[0]["result"]
    assert result.get("status") == "queued", observations
    assert observations[0]["sid"] == child and observations[0]["key"] == entry.session_key
    assert observations[0]["thread"] not in {item[3] for item in model_turns}


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["cache-agent", "worker", "agent-home", "ambient-home", "generation"])
async def test_native_agent_consumer_refuses_inconsistent_execution(shared_native, monkeypatch, mismatch):
    from gateway.session_context import _SESSION_ID
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    service, _, _, entry = shared_native
    runner = service.runner
    generation = runner._begin_session_run_generation(entry.session_key)
    agent = SimpleNamespace(session_id=entry.session_id, _session_db=SimpleNamespace(db_path=service.home / "state.db"))
    ctx = SimpleNamespace(session_key=entry.session_key, session_id=entry.session_id, run_generation=generation)

    async def consume(*a, **kw):
        token = set_hermes_home_override(service.home / "foreign") if mismatch == "ambient-home" else None
        mutations = {
            "cache-agent": lambda: setattr(agent, "session_id", "arbitrary-cached-sid"),
            "worker": lambda: _SESSION_ID.set("arbitrary-worker-sid"),
            "agent-home": lambda: setattr(agent._session_db, "db_path", service.home / "foreign" / "state.db"),
            "generation": lambda: runner._invalidate_session_run_generation(entry.session_key),
            "ambient-home": lambda: None,
        }
        mutations[mismatch]()
        try:
            runner._verify_native_turn_agent(ctx, agent)
        finally:
            if token is not None:
                reset_hermes_home_override(token)

    monkeypatch.setattr(runner, "_run_agent_inner", consume)
    with pytest.raises(PermissionError, match="resolved agent"):
        await runner._run_agent("offline", "", [], entry.origin, entry.session_id,
            session_key=entry.session_key, run_generation=generation)
    assert service.journal.pending() == []
    with service.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["store", "cache", "cache-snapshot", "result", "key", "agent-home", "raced-generation"])
async def test_followup_adoption_cross_checks_publications(shared_native, monkeypatch, mismatch):
    from hermes_state import SessionDB
    from gateway.platforms.event import MessageEvent
    service, _, _, entry = shared_native
    runner = service.runner
    db = SessionDB(service.home / "state.db")
    parent, child = entry.session_id, entry.session_id + "-candidate"
    if db.get_session(parent) is None:
        db.create_session(parent, source="discord")
    db.end_session(parent, "compression")
    db.create_session(child, source="discord", parent_session_id=parent)
    entry.session_id = child
    runner.session_store._save()
    generation = runner._begin_session_run_generation(entry.session_key)
    agent = SimpleNamespace(session_id=child, _session_db=db)
    ctx = SimpleNamespace(source=entry.origin, session_key=entry.session_key, session_id=parent,
        run_generation=generation, agent_holder=[agent])
    result = {"session_id": child}
    runner._agent_cache = {entry.session_key: (agent, "sig", 0, parent)}
    runner._agent_cache_lock = threading.Lock()
    pending = MessageEvent(text="followup", source=entry.origin)
    lookup = runner.async_session_store.lookup_by_session_key

    async def raced_lookup(key):
        found = await lookup(key)
        runner._invalidate_session_run_generation(key)
        return found

    async def consume(*a, **kw):
        mutations = {
            "store": lambda: setattr(entry, "session_id", "unrelated-store-sid"),
            "cache": lambda: runner._agent_cache.update({entry.session_key: (SimpleNamespace(session_id=child), "sig", 0, parent)}),
            "cache-snapshot": lambda: runner._agent_cache.update({entry.session_key: (agent, "sig", 0, "arbitrary-cache-sid")}),
            "result": lambda: result.update(session_id="arbitrary-result-sid"),
            "key": lambda: setattr(pending, "source", replace_source(entry.origin, chat_id="another-thread", thread_id="another-thread")),
            "agent-home": lambda: setattr(agent, "_session_db", SimpleNamespace(db_path=service.home / "foreign" / "state.db")),
            "raced-generation": lambda: monkeypatch.setattr(runner.async_session_store, "lookup_by_session_key", raced_lookup),
        }
        mutations[mismatch]()
        before = runner._agent_cache[entry.session_key]
        with pytest.raises(PermissionError, match="queued"):
            await runner._effective_followup_session_id(ctx, result, pending)
        assert runner._agent_cache[entry.session_key] is before

    monkeypatch.setattr(runner, "_run_agent_inner", consume)
    try:
        await runner._run_agent("offline", "", [], entry.origin, parent,
            session_key=entry.session_key, run_generation=generation)
        assert service.journal.pending() == []
    finally:
        db.close()
