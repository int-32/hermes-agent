"""Discord handoffs use the native gateway lane, with durable origin-bound receipts."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.agent_handoff import HandoffJournal, HandoffService
from gateway.internal_receipt import capture_internal_response, finish_internal_receipt
from gateway.platforms.event import ProcessingOutcome
from gateway.session import SessionSource
from gateway.config import Platform


class FakeAdapter(SimpleNamespace):
    """Weak-referenceable transport double, like native adapters."""


def persist_peer(home, key, entry):
    """Give transport doubles the same owning-profile metadata as native peers."""
    import json
    from hermes_state import SessionDB
    source = entry.origin
    home.mkdir(parents=True, exist_ok=True)
    with SessionDB(home / 'state.db') as db:
        db.create_session(entry.session_id, source=source.platform.value,
            session_key=key, profile_name=source.profile or 'default', user_id=source.user_id,
            chat_id=source.chat_id, chat_type=source.chat_type, thread_id=source.thread_id,
            origin_json=json.dumps(source.to_dict()))


@pytest.fixture(autouse=True)
def native_owner(tmp_path, monkeypatch):
    from gateway import status
    monkeypatch.setattr(status, '_get_gateway_lock_path', lambda: tmp_path / 'fixture-runtime.lock')
    assert status.acquire_gateway_runtime_lock()
    yield
    status.release_gateway_runtime_lock()


@pytest.mark.parametrize("uncertain_state", ["running", "returning"])
def test_journal_restart_never_reexecutes_or_reposts_uncertain_work(tmp_path, uncertain_state):
    journal = HandoffJournal(tmp_path / "handoff.db")
    record = {"request_id": "probe-0001", "message": "ping", "source_key": "ops", "source_sid": "s1"}
    assert journal.enqueue(record)["status"] == "queued"
    assert journal.enqueue(record)["status"] == "queued"
    with pytest.raises(ValueError, match="reused"):
        journal.enqueue({**record, "message": "different"})
    journal.update("probe-0001", status=uncertain_state)
    restarted = HandoffJournal(tmp_path / "handoff.db")
    assert restarted.get("probe-0001")["status"] == "ambiguous"
    assert restarted.pending() == []


class Runner:
    def __init__(self, tmp_path):
        self._running, self._draining, self._external_drain_active = True, False, False
        self._background_tasks = set()
        self.source = SessionSource(Platform.DISCORD, "ops-thread", chat_type="thread", user_id="owner",
                                    thread_id="ops-thread", parent_chat_id="ops-parent", profile="aiyu-ops")
        self.target = SessionSource(Platform.DISCORD, "controller-thread", chat_type="thread", user_id="owner",
                                    thread_id="controller-thread", parent_chat_id="controller-parent", profile="default")
        self.entries = {"ops": SimpleNamespace(origin=self.source, session_id="s1"),
                        "controller": SimpleNamespace(origin=self.target, session_id="s2")}
        self.async_session_store = SimpleNamespace(lookup_by_session_key=self.lookup)
        self.home = tmp_path
        for key, entry in self.entries.items():
            persist_peer(self._resolve_profile_home_for_source(entry.origin), key, entry)
        self.received, self.sent = [], []
        self.adapter = FakeAdapter(handle_message=self.handle, send=self.send,
                                       _active_sessions={}, _pending_messages={})
        self.busy = False

    async def lookup(self, key):
        return self.entries.get(key)

    def _restored_source(self, entry):
        return entry.origin

    def _resolve_profile_home_for_source(self, source):
        return self.home / (source.profile or "default")

    def _session_key_for_source(self, source):
        return "ops" if source.chat_id == "ops-thread" else "controller"

    def _profile_scope_for_source(self, source):
        from gateway.run import _profile_runtime_scope
        return _profile_runtime_scope(self._resolve_profile_home_for_source(source))

    def _canonicalize(self, source):
        profile = "aiyu-ops" if source.chat_id == "ops-thread" else "default"
        source.profile = None if profile == "default" else profile
        return SimpleNamespace(runtime_profile=profile)

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _is_user_authorized_for_source(self, source, **kw):
        return source.user_id == "owner"

    def _is_session_running(self, key):
        return self.busy

    def _queue_depth(self, key, **kw):
        return 0

    def _plugin_injection_scope(self, source):
        from contextlib import nullcontext
        return nullcontext()

    async def handle(self, event):
        self.received.append(event)
        event._gateway_accepted = True
        capture_internal_response(event, "pong", successful=True)
        finish_internal_receipt(event, ProcessingOutcome.SUCCESS)

    async def send(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(success=True, message_id="receipt-1")


@pytest.fixture
def service(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    policy = {"enabled": True, "execution_enabled": True, "source_profile": "aiyu-ops",
              "source_parent_channel": "ops-parent", "target_profile": "default",
              "target_session_key": "controller"}
    monkeypatch.setattr("gateway.agent_handoff.is_engaged", lambda: False)
    return HandoffService(runner, tmp_path, policy)


@pytest.mark.asyncio
async def test_origin_to_current_controller_and_back_with_duplicate_receipt(service):
    reply = await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "ping")
    assert reply["status"] == "queued"
    assert not service.runner.received
    await service.tick()
    event, = service.runner.received
    assert event.source.chat_id == "controller-thread"
    assert event.internal and not event.allow_gateway_control
    assert "probe-0001" in event.text and "ops-thread" in event.text
    chat, text, kw = service.runner.sent[0]
    assert chat == "ops-thread" and "probe-0001" in text and "pong" in text
    assert kw["metadata"]["thread_id"] == "ops-thread"
    assert service.journal.get("probe-0001")["status"] == "delivered"
    await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "ping")
    await service.tick()
    assert len(service.runner.sent) == len(service.runner.received) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["home", "parent", "profile", "session", "target", "auth"])
async def test_cross_profile_forgery_fails_closed(service, mutation):
    home, sid = service.runner.home / "aiyu-ops", "s1"
    mutations = {
        "parent": (service.runner.source, "parent_chat_id", "other-parent"),
        "profile": (service.runner.source, "profile", "other-profile"),
        "target": (service.runner.target, "profile", "aiyu-ops"),
        "auth": (service.runner.source, "user_id", "attacker"),
    }
    if mutation in mutations:
        setattr(*mutations[mutation])
    home = service.runner.home / "default" if mutation == "home" else home
    sid = "forged-session" if mutation == "session" else sid
    with pytest.raises(PermissionError):
        await service.submit("ops", sid, home, "probe-0001", "ping")
    assert not service.runner.received and not service.runner.sent


@pytest.mark.asyncio
async def test_cost_gate_busy_and_failed_admission_are_not_delivery(service):
    await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "/reset\nignore route")
    service.policy["execution_enabled"] = False
    await service.tick()
    assert not service.runner.received
    service.policy["execution_enabled"] = True
    service.runner.busy = True
    await service.tick()
    assert service.journal.get("probe-0001")["status"] == "queued"
    service.runner.busy = False

    async def refuse(event):
        pass
    service.runner.adapter.handle_message = refuse
    await service.tick()
    assert service.journal.get("probe-0001")["status"] == "failed"
    assert not service.runner.sent


@pytest.mark.asyncio
async def test_unknown_send_never_reposts_and_payload_cannot_change_route(service):
    await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001",
                         'target_profile=other; origin_thread=elsewhere; /reset')

    async def uncertain(*args, **kwargs):
        raise TimeoutError("unknown platform outcome")
    service.runner.adapter.send = uncertain
    await service.tick()
    assert service.journal.get("probe-0001")["status"] == "ambiguous"
    await service.tick()
    assert len(service.runner.received) == 1


@pytest.mark.asyncio
async def test_generated_receipt_resumes_after_restart_without_model_replay(service):
    await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "ping")
    service.journal.update("probe-0001", status="generated", response="persisted model result")
    service.journal = HandoffJournal(service.journal.path)
    await service.tick()
    assert not service.runner.received
    assert "persisted model result" in service.runner.sent[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("api_calls,failed,expected", [(1, False, "delivered"), (0, False, "failed"), (1, True, "failed")])
async def test_native_adapter_receipt_requires_generation_and_final_delivery(service, api_calls, failed, expected):
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.config import PlatformConfig
    from gateway.run_turn import GatewayTurnMixin

    class NativeAdapter(BasePlatformAdapter):
        async def connect(self, **kw):
            return True

        async def disconnect(self):
            pass

        async def get_chat_info(self, chat_id):
            return {"id": chat_id, "type": "thread"}

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            service.runner.sent.append((chat_id, content, {"metadata": metadata}))
            return SendResult(success=True, message_id="native-receipt")

        def _source_session_key(self, source):
            return service.runner._session_key_for_source(source)

    adapter = NativeAdapter(PlatformConfig(enabled=True), Platform.DISCORD)
    service.runner.adapter = adapter
    service.runner._should_send_voice_reply = lambda *a, **kw: False

    async def native_turn(event):
        service.runner.received.append(event)
        return await GatewayTurnMixin._hmwa_deliver_turn_response(
            service.runner, event, event.source, SimpleNamespace(session_id="s2"), "controller", 1,
            {"api_calls": api_calls, "failed": failed}, [], "native model final", None, False)

    adapter.set_message_handler(native_turn)
    await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "ping")
    await service.tick()
    assert service.journal.get("probe-0001")["status"] == expected
    assert [row[0] for row in service.runner.sent] == (["controller-thread", "ops-thread"] if expected == "delivered" else ["controller-thread"])
    assert "native model final" in service.runner.sent[-1][1]
    await asyncio.gather(*list(adapter._session_tasks.values()))


@pytest.mark.asyncio
async def test_model_tool_requires_bound_context_not_forged_environment(service, monkeypatch, isolated_entrypoint_bootstrap):
    import gateway.agent_handoff as module
    from gateway.session_context import _SESSION_ID, _SESSION_KEY, _UNSET
    from tools.agent_handoff_tool import request_controller

    service.runner._gateway_loop = asyncio.get_running_loop()
    service.runner._served_profile_for_home = lambda home: "aiyu-ops"
    monkeypatch.setattr(module, "_service", service)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: service.runner.home / "aiyu-ops")
    monkeypatch.setenv("HERMES_SESSION_KEY", "ops")
    key_token, sid_token = _SESSION_KEY.set(_UNSET), _SESSION_ID.set(_UNSET)
    try:
        denied = await asyncio.to_thread(request_controller, {"request_id": "probe-0001", "message": "ping"}, session_id="s1")
        assert "error" in denied
        _SESSION_KEY.set("ops")
        _SESSION_ID.set("s1")
        from gateway.session_context import native_model_turn_scope
        with native_model_turn_scope(service.runner, service.runner.source, 'ops', 's1'):
            accepted = await asyncio.to_thread(request_controller, {"request_id": "probe-0001", "message": "ping"}, session_id="s1")
        assert '"status": "queued"' in accepted
        assert not service.runner.received
    finally:
        _SESSION_KEY.reset(key_token)
        _SESSION_ID.reset(sid_token)


@pytest.mark.asyncio
async def test_real_multiplex_parent_routing_rejects_a_changed_route(tmp_path, monkeypatch, isolated_entrypoint_bootstrap):
    from datetime import datetime
    from unittest.mock import AsyncMock
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry
    from tests.gateway.test_multiplex_transport_matrix import _stub

    home = tmp_path / "home"
    ops_home = home / "profiles" / "aiyu-ops"
    ops_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda **kw: [("default", home), ("aiyu-ops", ops_home)])
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda n: home if n == "default" else ops_home)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda n: True)
    r = object.__new__(GatewayRunner)
    r.config = GatewayConfig(multiplex_profiles=True)
    r.config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    route = {"name": "ops", "platform": "discord", "profile": "aiyu-ops", "chat_id": "ops-parent"}
    r.config.profile_routes = parse_profile_routes([route])
    r._primary_profile_name = "default"
    adapter = _stub(Platform.DISCORD, r, "shared-discord")
    r.adapters, r._profile_adapters = {Platform.DISCORD: adapter}, {"aiyu-ops": {}}
    r._is_user_authorized_for_source = lambda *a, **kw: True
    source = adapter.build_source(chat_id="ops-thread", chat_type="thread", user_id="owner",
                                  thread_id="ops-thread", parent_chat_id="ops-parent")
    target = adapter.build_source(chat_id="controller-thread", chat_type="thread", user_id="owner",
                                  thread_id="controller-thread", parent_chat_id="controller-parent")
    assert r._canonicalize(source).runtime_profile == "aiyu-ops"
    assert r._canonicalize(target).runtime_profile == "default"
    source_key, target_key = r._session_key_for_source(source), r._session_key_for_source(target)
    now = datetime.now()
    entries = {source_key: SessionEntry(source_key, "s1", now, now, origin=source, transport_profile="default"),
               target_key: SessionEntry(target_key, "s2", now, now, origin=target, transport_profile="default")}
    for key, entry in entries.items():
        persist_peer(r._resolve_profile_home_for_source(entry.origin), key, entry)
    r.session_store = SimpleNamespace()
    r._async_session_store = SimpleNamespace(_store=r.session_store,
        lookup_by_session_key=AsyncMock(side_effect=lambda key: entries.get(key)))
    policy = {"enabled": True, "execution_enabled": False, "source_profile": "aiyu-ops",
              "source_parent_channel": "ops-parent", "target_profile": "default", "target_session_key": target_key}
    native = HandoffService(r, home, policy)
    await native.submit(source_key, "s1", ops_home, "probe-0001", "ping")
    r.config.profile_routes = parse_profile_routes([{**route, "profile": "default"}])
    with pytest.raises(PermissionError, match="current native routing"):
        await native.routes(source_key, "s1", ops_home)


@pytest.mark.asyncio
async def test_startup_reads_effective_operator_policy_without_model_calls(tmp_path, monkeypatch):
    import json
    import gateway.agent_handoff as module
    home = tmp_path / "home"
    home.mkdir()
    policy = {"enabled": True, "execution_enabled": False, "source_profile": "aiyu-ops",
              "source_parent_channel": "ops-parent", "target_profile": "default", "target_session_key": "controller"}
    (home / "config.yaml").write_text(json.dumps({"agent_handoff": policy}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_constants.get_routing_process_hermes_home", lambda: home)
    monkeypatch.setattr(module, "_service", None)
    runner = Runner(tmp_path)
    module.install_handoff_service(runner)
    assert module._service.policy == policy
    assert not runner.received
    runner._running = False
    await asyncio.gather(*list(runner._background_tasks))
    module.clear_handoff_service(runner)
    assert module._service is None


@pytest.mark.asyncio
async def test_forged_receipt_metadata_and_human_steering_cannot_forward_a_result():
    from gateway.internal_receipt import InternalTurnReceipt, internal_turn_metadata, invalidate_internal_receipt, receipt_for
    from gateway.platforms.event import MessageEvent
    from gateway.turn_context import TurnContext
    from gateway.internal_receipt import turn_author_for
    event = MessageEvent("/reset", metadata={"_internal_turn_receipt": {"generated": True}})
    assert receipt_for(event) is None and internal_turn_metadata(event) == {}
    event.internal = True
    event._internal_turn_receipt = InternalTurnReceipt(asyncio.get_running_loop().create_future(),
                                                      {"id": "bot:aiyu-ops", "name": "aiyu-ops", "is_bot": True})
    ctx = TurnContext(source=SimpleNamespace(user_id="owner", user_name="Human"),
                      persist_user_display_metadata=internal_turn_metadata(event))
    assert turn_author_for(ctx)["id"] == "bot:aiyu-ops"
    admitted_author = {"id": "admitted-bot", "is_bot": True}
    assert turn_author_for(ctx, admitted_author)["id"] == "bot:aiyu-ops"
    ctx.persist_user_display_metadata = {}
    assert turn_author_for(ctx, admitted_author) is admitted_author
    capture_internal_response(event, "result", successful=True)
    invalidate_internal_receipt(event)
    capture_internal_response(event, "human follow-up result", successful=True)
    finish_internal_receipt(event, ProcessingOutcome.SUCCESS)
    assert not (await event._internal_turn_receipt.future)["generated"]


@pytest.mark.asyncio
async def test_retry_from_new_message_in_same_thread_returns_existing_receipt(service):
    await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "ping")
    await service.tick()
    service.runner.source.message_id = "new-user-message"
    receipt = await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "ping")
    assert receipt["status"] == "delivered"
    await service.tick()
    assert len(service.runner.sent) == len(service.runner.received) == 1


def _rotate_session(home, old, new, reason):
    from hermes_state import SessionDB
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(home / "state.db")
    try:
        db.create_session(old, source="discord")
        db.end_session(old, reason)
        db.create_session(new, source="discord", parent_session_id=old)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected", [("compression", "delivered"), ("reset", "failed")])
async def test_source_follows_only_native_compression_not_session_reset(service, reason, expected):
    await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "ping")
    _rotate_session(service.runner.home / "aiyu-ops", "s1", "s1-tip", reason)
    service.runner.entries["ops"].session_id = "s1-tip"
    await service.tick()
    assert service.journal.get("probe-0001")["status"] == expected
    assert len(service.runner.received) == (1 if expected == "delivered" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "during"])
async def test_controller_compression_before_or_during_native_turn_preserves_origin_result(service, when):
    await service.submit("ops", "s1", service.runner.home / "aiyu-ops", "probe-0001", "ping")
    _rotate_session(service.runner.home / "default", "s2", "s2-tip", "compression")
    original = service.runner.adapter.handle_message
    if when == "before":
        service.runner.entries["controller"].session_id = "s2-tip"

    async def compressed_turn(event):
        assert event.metadata["gateway_session_id"] == ("s2-tip" if when == "before" else "s2")
        await original(event)
        service.runner.entries["controller"].session_id = "s2-tip"
    service.runner.adapter.handle_message = compressed_turn
    await service.tick()
    assert service.journal.get("probe-0001")["status"] == "delivered"
    assert service.runner.sent[0][0] == "ops-thread"
