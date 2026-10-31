"""Live native tools must see compression published inside the same model conversation."""
import asyncio
from collections import OrderedDict
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from gateway.agent_coordination import submit_document
from gateway.config import Platform
from gateway.coordination_receipts import DocumentTurn, document_turn, settlement_changes
from tests.gateway.test_native_queued_turn_binding import _response
from tests.gateway import native_endpoint_fixtures as source_fixtures
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

shared_native = source_fixtures.shared_native

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("isolated_entrypoint_bootstrap")]


@pytest_asyncio.fixture
async def live_model(shared_native, monkeypatch, request):
    import gateway.run as gateway_run
    import model_tools
    import run_agent
    from hermes_state import SessionDB, AsyncSessionDB
    from gateway.session_context import _SESSION_ID
    from gateway import session_context as sc
    from tools.registry import registry

    service, path, _, entry = shared_native
    runner = service.runner
    task_entry = entry
    if getattr(request.node, "callspec", None) and request.node.callspec.params.get("phase") == "continuation":
        entry = await runner.async_session_store.lookup_by_session_key(service.policy["coordination"]["mains"]["default"]["session_key"])
    db = SessionDB(service.home / "state.db")
    runner._session_db = AsyncSessionDB(db)
    runner._agent_cache, runner._agent_cache_lock = OrderedDict(), threading.Lock()
    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    adapter.gateway_runner = runner
    for name, value in _make_runner(adapter).__dict__.items():
        runner.__dict__.setdefault(name, value)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._service_tier = None
    runner._pinned_session_context_prompt = lambda *a, **kw: ""
    runner._resolve_session_agent_runtime = lambda **kw: ("offline-script", {
        "api_key": "offline", "provider": "openai", "api_mode": "chat_completions",
        "base_url": "http://127.0.0.1:1/v1"})
    runner._run_post_turn_hooks = AsyncMock()
    agents, observed, windows = [], [], []
    plan = SimpleNamespace(args=None, tool="coordinate_session", transition="compression", calls=0, mutate=None,
                           after_tool=None, service=service, keys={entry.session_key})

    class ScriptedAgent(run_agent.AIAgent):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "skip_memory": True, "skip_context_files": True,
                                "load_soul_identity": False, "save_trajectories": False})
            self.compression_enabled = False
            self.compression_in_place = False
            self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.script)))
            compressor = MagicMock()
            compressor.compress.return_value = [
                {"role": "user", "content": "[CONTEXT COMPACTION] scripted summary"},
                {"role": "assistant", "content": "ready to finish private work"}]
            compressor.compression_count = 1
            compressor.last_prompt_tokens = compressor.last_completion_tokens = 0
            compressor._last_summary_error = compressor._last_aux_model_failure_model = None
            compressor._last_aux_model_failure_error = None
            compressor._last_compress_aborted = compressor._last_summary_fallback_used = False
            compressor._last_compression_made_progress = True
            self.context_compressor = compressor
            self._compression_feasibility_checked = True
            agents.append(self)

        def _create_request_openai_client(self, **kwargs):
            return self.client

        def script(self, **kwargs):
            plan.calls += 1
            if plan.calls == 1:
                return _response(tool_calls=[SimpleNamespace(id="active-call", type="function",
                    function=SimpleNamespace(name=plan.tool, arguments=json.dumps(plan.args)))])
            if plan.after_tool:
                plan.after_tool()
            return _response("private work final")

    from agent import turn_context_compaction
    turn_start = turn_context_compaction.run_turn_start_compaction

    def scripted_compaction(agent, *a, **kw):
        out = turn_start(agent, *a, **kw)
        old = agent.session_id
        if plan.transition != "none":
            # This runs INSIDE real run_conversation, before the tool executor. Never
            # synchronize entry or postpone the rotation until the conversation returns.
            out.messages, out.active_system_prompt = agent._compress_context(
                out.messages, agent._cached_system_prompt, approx_tokens=120_000, force=True)
            assert agent.session_id != old
            if plan.transition in {"reset", "fork"}:
                reason = "session_reset" if plan.transition == "reset" else "compression"
                db._write_sql("UPDATE sessions SET end_reason=? WHERE id=?", (reason, old))
                if plan.transition == "fork":
                    db._write_sql("UPDATE sessions SET model_config=? WHERE id=?",
                                  (json.dumps({"_branched_from": old}), agent.session_id))
            elif plan.transition == "generation":
                async def invalidate():
                    runner._invalidate_session_run_generation(entry.session_key)
                asyncio.run_coroutine_threadsafe(invalidate(), runner._gateway_loop).result(5)
        if plan.mutate:
            plan.mutate(agent, db)
        windows.append({"entry_sid": entry.session_id, "agent_sid": agent.session_id,
                        "worker_sid": _SESSION_ID.get(), "tip": db.get_compression_tip(old),
                        "thread": threading.current_thread()})
        return out

    monkeypatch.setattr(turn_context_compaction, "run_turn_start_compaction", scripted_compaction)
    monkeypatch.setattr(run_agent, "AIAgent", ScriptedAgent)
    monkeypatch.setattr(gateway_run, "_hermes_home", service.home)
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **kw: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"model": "offline-script", "display": {"tool_progress": "off"}})
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *a: {"agent_handoff"})
    schemas = [{"type": "function", "function": registry.get_entry(name).schema}
               for name in ("coordinate_session", "request_controller")]
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: schemas)
    original = model_tools.handle_function_call

    def dispatch(*a, **kw):
        from gateway import agent_handoff
        from gateway.session_context import _SESSION_KEY
        assert agent_handoff._service is plan.service
        assert agent_handoff.handoff_available(coordination=True)
        assert _SESSION_KEY.get() in plan.keys
        result = original(*a, **kw)
        observed.append({"result": json.loads(result), "passed_sid": kw.get("session_id"),
                         "sid": _SESSION_ID.get(), "source": getattr(sc, "_SESSION_NATIVE_SOURCE", None).get() if hasattr(sc, "_SESSION_NATIVE_SOURCE") else None,
                         "thread": threading.current_thread()})
        return result

    monkeypatch.setattr(model_tools, "handle_function_call", dispatch)

    async def run():
        plan.calls = 0
        generation = runner._begin_session_run_generation(entry.session_key)
        return await runner._run_agent("finish private native work " + "prior context " * 300, "", [], entry.origin, entry.session_id,
            session_key=entry.session_key, run_generation=generation)

    yield SimpleNamespace(service=service, runner=runner, path=path, entry=entry, db=db,
                          plan=plan, run=run, observed=observed, windows=windows, agents=agents, task_entry=task_entry)
    for agent in agents:
        agent.close()
    db.close()


async def receiving_turn(model, phase):
    service, entry, task = model.service, model.entry, model.task_entry
    policy = service.policy["coordination"]
    main = policy["mains"]["default"]
    if phase == "request":
        source_key, source_sid, target = main["session_key"], main["session_id"], "task:task-one"
    else:
        source_key, source_sid, target = task.session_key, task.session_id, "main"
    await submit_document(service, source_key, source_sid, service.home,
                          "active-parent-work", "task-one", str(model.path), target)
    work = service.journal.update("active-parent-work", status="running")
    turn = DocumentTurn(work, entry.session_key, entry.session_id, "request")
    if phase == "continuation":
        async with document_turn(turn):
            await submit_document(service, entry.session_key, entry.session_id, service.home,
                                  "active-ops-child", "task-one", str(model.path), "peer")
        child = service.journal.update("active-ops-child", status="delivered", outcome="completed")
        turn.work = service.journal.update(work["request_id"], status="waiting_children")
        turn.phase, turn.child = "continuation", child
    return turn


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["request", "continuation"])
async def test_complete_immediately_after_real_in_conversation_compression(live_model, phase):
    model = live_model
    old = model.entry.session_id
    turn = await receiving_turn(model, phase)
    original = model.service.journal.get(turn.work["request_id"])
    model.plan.args = {"action": "complete", "request_id": turn.work["request_id"], "outcome": "completed"}
    async with document_turn(turn):
        await model.run()
    window = model.windows[0]
    assert window["entry_sid"] == old and window["agent_sid"] == window["worker_sid"] == window["tip"] != old
    assert model.observed[0]["thread"] is not window["thread"], "must dispatch through the real tool worker"
    result = model.observed[0]["result"]
    assert result.get("status") == "recorded", model.observed
    current = model.service.journal.get(turn.work["request_id"])
    for field in ("target_sid", "source_sid", "document", "source_key", "target_key", "root_request_id"):
        assert current[field] == original[field]
    assert current["acknowledgements"][-1]["session_id"] == window["agent_sid"]
    assert turn.completion == "completed"
    assert current["acknowledgements"][-1]["dependency_request_id"] == (turn.child["request_id"] if turn.child else None)
    changes = await settlement_changes(model.service, current, turn, "private work final")
    assert changes["outcome"] == "completed" and changes["status"] == "generated"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["request", "complete"])
@pytest.mark.parametrize("transition", ["none", "compression", "reset", "fork", "generation"])
async def test_all_native_consumers_share_active_endpoint_contract(live_model, action, transition):
    model = live_model
    model.plan.transition = transition
    old = model.entry.session_id
    if action == "complete":
        turn = await receiving_turn(model, "request")
        model.plan.args = {"action": "complete", "request_id": turn.work["request_id"], "outcome": "completed"}
    else:
        turn = None
        model.plan.args = {"action": action, "task_id": "task-one", "handoff_path": str(model.path),
                           "request_id": "active-native-request", "target": "main"}
    before = model.service.journal.pending()
    async with document_turn(turn):
        run_result = await model.run()
    if not model.observed:
        assert transition in {"reset", "fork"} and run_result["failed"], run_result
        assert model.service.journal.pending() == before
        assert not turn or turn.completion is None
        return
    result = model.observed[0]["result"]
    window = model.windows[0]
    assert window["entry_sid"] == old
    assert model.observed[0]["sid"] == model.observed[0]["passed_sid"] == window["agent_sid"]
    if transition in {"none", "compression"}:
        expected = {"request": "queued", "complete": "recorded"}[action]
        assert result.get("status") == expected, model.observed
        if action == "request":
            record = model.service.journal.get(model.plan.args["request_id"])
            assert record["source_sid"] == window["agent_sid"]
            assert record["origin"]["chat_id"] == model.entry.origin.chat_id
    else:
        assert "error" in result, model.observed
        assert model.service.journal.pending() == before
        if action != "complete":
            assert model.service.journal.get(model.plan.args["request_id"]) is None
        else:
            assert turn.completion is None
        if action == "complete":
            assert not model.service.journal.get(turn.work["request_id"]).get("acknowledgements")


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["key", "owner", "profile", "origin", "ended", "home", "entry"])
async def test_live_compression_requires_consistent_publication(live_model, damage):
    model = live_model
    model.plan.args = {"action": "request", "request_id": "publication-damage", "target": "main", "task_id": "task-one", "handoff_path": str(model.path)}

    def corrupt(agent, db):
        changes = {"key": ("session_key", "arbitrary-key"), "owner": ("user_id", "another-owner"),
                   "profile": ("profile_name", "aiyu-ops"), "origin": ("origin_json", "{}"),
                   "empty": ("message_count", 0), "ended": ("ended_at", 1)}
        if damage in changes:
            column, value = changes[damage]
            db._write_sql(f"UPDATE sessions SET {column}=? WHERE id=?", (value, agent.session_id))
        elif damage == "home":
            class ForeignDB:
                db_path = model.service.home / "wrong" / "state.db"

                def __getattr__(self, name):
                    return getattr(db, name)

            agent._session_db = ForeignDB()
        elif damage == "agent":
            agent.session_id = "unpublished-cache-id"
        else:
            # A route reset is an external invalidation, never an entry repair for this test.
            model.entry.session_id = "reset-by-other-owner"

    model.plan.mutate = corrupt
    try:
        await model.run()
    except (AttributeError, PermissionError):
        assert damage in {"home", "entry"}
    assert model.observed and "error" in model.observed[0]["result"], model.observed
    from hermes_cli.kanban_task_source import read_sources
    assert list(read_sources(model.service.home)) == ["task-one"]
    # Restore only the test double's DB reference so its close() can release resources.
    for agent in model.agents:
        agent._session_db = model.db
