"""Real FIFO recursion and model/tool workers must bind the actual turn's requester."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
@pytest.mark.parametrize("first,queued", [("owner", "colleague"), ("colleague", "owner")])
@pytest.mark.parametrize("action", ["request"])
async def test_busy_fifo_recursive_model_tool_uses_queued_requester(shared_native, monkeypatch, first, queued, action):
    import gateway.run as gateway_run
    import model_tools
    import run_agent
    from gateway.session_context import _SESSION_NATIVE_SOURCE, _SESSION_ID, _SESSION_KEY
    from tools.registry import registry
    from run_agent import AIAgent

    service, path, _, entry = shared_native
    runner = service.runner
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
            agents.append(self)

        def _create_request_openai_client(self, **kwargs):
            return self.client

        def script(self, **kwargs):
            messages = kwargs["messages"]
            user = next(str(m["content"]) for m in reversed(messages) if m["role"] == "user")
            model_turns.append((self._user_id, _SESSION_NATIVE_SOURCE.get().user_id, threading.current_thread()))
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
                             "key": _SESSION_KEY.get(), "sid": _SESSION_ID.get(), "thread": threading.current_thread()})
        return result

    monkeypatch.setattr(model_tools, "handle_function_call", observe_dispatch)
    initial = replace_source(entry.origin, user_id=first)
    follower = replace_source(entry.origin, user_id=queued)
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
        assert pending.source.user_id == queued and runner._queue_depth(entry.session_key, adapter=adapter) == 1
        release.set()
        await asyncio.wait_for(task, 30)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for agent in agents:
            agent.close()
        assert old_adapter is not None  # retain the original real intake provenance until cleanup
    assert observations, model_turns
    result = observations[0]["result"]
    if queued == "owner":
        assert result.get("status") == "queued", observations
    else:
        assert "error" in result, observations
        from hermes_cli.kanban_task_source import read_sources
        assert list(read_sources(service.home)) == ["task-one"]
        assert service.journal.get("queued-native-request") is None
        with service.journal.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert all(actor == snapshot for actor, snapshot, _ in model_turns)
    assert observations[0]["source"] == queued
    assert observations[0]["key"] == entry.session_key and observations[0]["sid"] == entry.session_id
    assert observations[0]["thread"] not in {item[2] for item in model_turns}


def _native_snapshot():
    from gateway import session_context as sc
    from hermes_constants import get_hermes_home
    from agent.runtime_cwd import scoped_session_cwd
    from gateway.native_endpoint import NATIVE_TURN
    return tuple(var.get() for var in (sc._SESSION_NATIVE_SOURCE, sc._SESSION_NATIVE_HOME,
        sc._SESSION_KEY, sc._SESSION_ID, sc._SESSION_USER_ID, sc._SESSION_PROFILE,
        sc._SESSION_ASYNC_DELIVERY, sc._SESSION_HISTORY_DELIVERY)) + (get_hermes_home(), scoped_session_cwd(), NATIVE_TURN.get())


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["normal", "error", "cancel"])
async def test_common_turn_boundary_restores_nested_identity_and_profile(shared_native, monkeypatch, outcome):
    from gateway.agent_coordination import HANDOFF_LINEAGE
    from gateway.coordination_receipts import DOCUMENT_TURN
    from gateway.session_context import _SESSION_ID
    from agent.runtime_cwd import set_session_cwd

    service, _, _, outer = shared_native
    runner = service.runner
    peer = await runner.async_session_store.lookup_by_session_key(service.policy["coordination"]["mains"]["aiyu-ops"]["session_key"])
    entered = asyncio.Event()
    restored = []
    document = object()

    async def inner(message, prompt, history, source, sid, **kwargs):
        assert DOCUMENT_TURN.get() is document and HANDOFF_LINEAGE.get() == ("ancestor",)
        if message == "outer":
            before = _native_snapshot()
            assert before[0].user_id == "owner" and before[1] == service.home
            try:
                return await runner._run_agent("inner", "", [], peer.origin, peer.session_id, session_key=peer.session_key)
            finally:
                assert _native_snapshot() == before
                assert _native_snapshot()[0] is before[0]
                assert _native_snapshot()[-1] is before[-1]
                restored.append("outer")
        actual = _native_snapshot()
        assert actual[0] is not source and actual[0].to_dict() == source.to_dict()
        assert actual[1] == runner._resolve_profile_home_for_source(source) == actual[-3]
        assert actual[2:6] == (peer.session_key, peer.session_id, "owner", "aiyu-ops")
        # A nested model can rotate its worker id/cwd without overwriting its caller's tokens.
        _SESSION_ID.set("nested-rotated-id")
        set_session_cwd("nested-working-directory")
        entered.set()
        if outcome == "error":
            raise ValueError("scripted nested failure")
        if outcome == "cancel":
            await asyncio.Future()
        return "nested result"

    monkeypatch.setattr(runner, "_run_agent_inner", inner)

    async def drive():
        baseline = _native_snapshot()
        doc_token, lineage_token = DOCUMENT_TURN.set(document), HANDOFF_LINEAGE.set(("ancestor",))
        try:
            return await runner._run_agent("outer", "", [], outer.origin, outer.session_id, session_key=outer.session_key)
        finally:
            assert _native_snapshot() == baseline
            assert DOCUMENT_TURN.get() is document and HANDOFF_LINEAGE.get() == ("ancestor",)
            DOCUMENT_TURN.reset(doc_token)
            HANDOFF_LINEAGE.reset(lineage_token)
            restored.append("baseline")

    task = asyncio.create_task(drive())
    if outcome == "cancel":
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "error":
        with pytest.raises(ValueError, match="scripted nested failure"):
            await task
    else:
        assert await task == "nested result"
    assert restored == ["outer", "baseline"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["key", "sid", "home", "outer-only"])
async def test_native_turn_proof_requires_its_own_key_sid_home(shared_native, monkeypatch, mismatch):
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.registry import registry

    service, path, _, entry = shared_native
    runner = service.runner
    args = {"action": "request", "request_id": "queued-native-request", "target": "main", "task_id": "task-one", "handoff_path": str(path)}

    async def consume(*a, **kw):
        token = set_hermes_home_override(service.home / "foreign") if mismatch == "home" else None
        try:
            return json.loads(await asyncio.to_thread(registry.dispatch, "coordinate_session", args,
                session_id="wrong-sid" if mismatch == "sid" else entry.session_id))
        finally:
            if token is not None:
                reset_hermes_home_override(token)

    monkeypatch.setattr(runner, "_run_agent_inner", consume)
    # Ambient ids/env and the outer preparation cannot stand in for a model-turn binding.
    tokens = set_session_vars(session_key=entry.session_key, session_id=entry.session_id)
    try:
        if mismatch == "outer-only":
            runner._set_session_env(SimpleNamespace(source=entry.origin, session_key=entry.session_key,
                                                    session_id=entry.session_id))
            result = await consume()
        else:
            kwargs = {} if mismatch == "key" else {"session_key": entry.session_key}
            result = await runner._run_agent("offline", "", [], entry.origin,
                "wrong-sid" if mismatch == "sid" else entry.session_id, **kwargs)
        assert "error" in result, result
    finally:
        clear_session_vars(tokens)
    assert service.journal.get("queued-native-request") is None
