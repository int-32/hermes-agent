"""Explicit two-main stars, document versions and origin-bound native receipts."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.agent_handoff import HandoffService
from gateway.agent_coordination import submit_document, HANDOFF_LINEAGE
from gateway.config import Platform
from gateway.session import SessionSource
from tests.gateway.test_agent_handoff import Runner, persist_peer, native_owner

pytestmark = pytest.mark.platforms("posix")


class StarRunner(Runner):
    async def handle(self, event):
        from gateway.coordination_receipts import DOCUMENT_TURN, complete_turn
        turn = DOCUMENT_TURN.get()
        if turn is not None:
            await complete_turn(self.service, event.source.chat_id,
                                self.entries[event.source.chat_id].session_id,
                                self._resolve_profile_home_for_source(event.source),
                                turn.work["request_id"], "blocked" if turn.child and turn.child.get("outcome") != "completed" else "completed")
        await super().handle(event)

    def __init__(self, root):
        super().__init__(root)
        self.entries = {}
        for key, profile, thread, parent in (
                ("default-main", "default", None, None), ("ops-main", "aiyu-ops", None, None),
                ("default-task", "default", "default-task", "projects"),
                ("ops-task", "aiyu-ops", "ops-task", "ops-parent")):
            source = SessionSource(Platform.DISCORD, key, chat_type="thread" if thread else "group",
                                   user_id="owner", thread_id=thread, parent_chat_id=parent,
                                   scope_id="guild", profile=profile)
            self.entries[key] = SimpleNamespace(origin=source, session_id=key + "-sid")
            persist_peer(self._resolve_profile_home_for_source(source), key, self.entries[key])

    def _session_key_for_source(self, source):
        return source.chat_id

    def _canonicalize(self, source):
        return SimpleNamespace(runtime_profile=self.entries[source.chat_id].origin.profile)


def make_star(tmp_path, monkeypatch, *, native_tasks=True):
    runner = StarRunner(tmp_path)
    root = tmp_path / "handoffs"
    path = root / "project" / "task-one" / "HANDOFF.md"
    path.parent.mkdir(parents=True)
    path.write_text("harmless nonce: model must actually read this", encoding="utf-8")
    mains = {p: {"session_key": k, "session_id": k + "-sid", "chat_id": k}
             for p, k in (("default", "default-main"), ("aiyu-ops", "ops-main"))}
    tasks = {n: {"profile": p, "session_key": k, "session_id": k + "-sid",
                 "chat_id": k, "parent_chat_id": parent, "project": "project"}
             for n, p, k, parent in (("task-one", "default", "default-task", "projects"),
                                     ("ops-one", "aiyu-ops", "ops-task", "ops-parent"))}
    policy = {"enabled": True, "execution_enabled": True, "coordination": {
        "enabled": True, "owner_user_id": "owner", "guild_id": "guild", "handoff_root": str(root),
        "mains": mains, "tasks": tasks, "task_parents": {"default": ["projects"], "aiyu-ops": ["ops-parent"]}}}
    monkeypatch.setattr("gateway.agent_handoff.is_engaged", lambda: False)
    service = HandoffService(runner, tmp_path, policy)
    monkeypatch.setattr("gateway.agent_handoff._service", service)
    runner.service = service
    from hermes_cli import projects_db
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from gateway import status
    assert status.acquire_gateway_runtime_lock()
    with projects_db.connect_closing() as conn:
        service.fixture_project_id = projects_db.create_project(conn, name='Project', slug='system', primary_path=str(tmp_path))
    if native_tasks:
        from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
        from hermes_cli.kanban_task_source import accept_task
        monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
        kb.create_board('registry', tracking_only=True)
        with kbc.connect_closing(board='registry') as conn:
            for task_id, spec in tasks.items():
                source = runner.entries[spec['session_key']].origin
                accept_task(conn, policy=policy['coordination'], source=source,
                            session_key=spec['session_key'], session_id=spec['session_id'],
                            profile_home=runner.home / spec['profile'], task_id=task_id,
                            document_project='project', project_id=service.fixture_project_id, title=task_id)
    return service, path


@pytest.fixture
def star(tmp_path, monkeypatch):
    return make_star(tmp_path, monkeypatch)

async def send(star, source, target, request="request-0001", task="task-one", path=None):
    service, document = star
    entry = service.runner.entries[source]
    return await submit_document(service, source, entry.session_id,
                                 service.runner.home / entry.origin.profile,
                                 request, task, str(path or document), target)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target,task", [
    ("default-task", "main", "task-one"), ("default-main", "task:task-one", "task-one"),
    ("ops-task", "main", "ops-one"), ("ops-main", "task:ops-one", "ops-one"),
    ("default-main", "peer", "task-one"), ("ops-main", "peer", "task-one")])
async def test_native_stars_all_directions_and_origin_receipt(star, source, target, task):
    service, path = star
    if task == "ops-one":
        path = path.parent.parent / task / "HANDOFF.md"
        path.parent.mkdir(); path.write_text("nonce ops")
    await send(star, source, target, task=task, path=path)
    record = service.journal.get("request-0001")
    assert record["status"] == "queued" and "harmless nonce" not in json.dumps(record)
    await service.tick()
    assert service.journal.get("request-0001")["status"] == "delivered"
    assert not service.runner.sent
    event, returned = service.runner.received
    assert returned.source.chat_id == source and "request-0001" in returned.text
    assert str(path) in event.text and record["document"]["sha256"] in event.text
    assert event._internal_turn_receipt.author["id"] == "bot:" + record["source_profile"]
    await send(star, source, target, task=task, path=path)
    await service.tick()
    assert len(service.runner.received) == 2 and not service.runner.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target,task", [
    ("default-task", "peer", "task-one"), ("default-task", "task:ops-one", "task-one"),
    ("default-main", "task:ops-one", "ops-one"), ("ops-main", "task:missing", "task-one"),
    ("default-task", "main", "ops-one")])
async def test_unregistered_cross_profile_and_wrong_task_edges_refused(star, source, target, task):
    with pytest.raises((PermissionError, ValueError)):
        await send(star, source, target, task=task)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["owner", "guild", "parent", "profile", "session", "home", "route"])
async def test_gateway_identity_and_binding_rechecked(star, mutation):
    service, path = star
    source = service.runner.entries["default-task"].origin
    changes = {"owner": (source, "user_id", "attacker"), "guild": (source, "scope_id", "other"),
               "parent": (source, "parent_chat_id", "other"), "profile": (source, "profile", "aiyu-ops"),
               "session": (service.runner.entries["default-task"], "session_id", "reset")}
    if mutation in changes:
        setattr(*changes[mutation])
    if mutation == "route":
        service.runner._canonicalize = lambda s: SimpleNamespace(runtime_profile="other")
    home = service.runner.home / ("aiyu-ops" if mutation == "home" else "default")
    with pytest.raises(PermissionError):
        await submit_document(service, "default-task", "default-task-sid", home,
                              "request-0001", "task-one", str(path), "main")


@pytest.mark.asyncio
async def test_document_escape_symlink_and_changed_version_refused(star, tmp_path):
    service, path = star
    outside = tmp_path / "outside.md"; outside.write_text("sensitive")
    for document in (outside, path.parent / ".." / "task-one" / "HANDOFF.md"):
        with pytest.raises((PermissionError, ValueError)):
            await send(star, "default-task", "main", path=document)
    path.unlink(); path.symlink_to(outside)
    with pytest.raises((PermissionError, ValueError, OSError)):
        await send(star, "default-task", "main")
    path.unlink(); path.write_text("version 1")
    await send(star, "default-task", "main")
    path.write_text("version 2")
    await service.tick()
    assert service.journal.get("request-0001")["status"] == "failed"
    assert not service.runner.received


@pytest.mark.asyncio
async def test_busy_restart_pins_old_target_and_refuses_changed_source(star):
    service, _ = star
    await send(star, "default-task", "main")
    service.runner.busy = True
    await service.tick()
    assert service.journal.get("request-0001")["status"] == "queued"
    restarted = HandoffService(service.runner, service.home, service.policy)
    service.runner.busy = False
    await restarted.tick()
    assert restarted.journal.get("request-0001")["status"] == "delivered"
    await send(star, "default-task", "main", request="request-0002")
    service.policy["coordination"]["mains"]["default"]["session_key"] = "ops-main"
    await service.tick()
    assert service.journal.get("request-0002")["status"] == "failed"
    assert len(service.runner.received) == 2


@pytest.mark.asyncio
async def test_internal_lineage_refuses_recursive_return_to_ancestor(star):
    token = HANDOFF_LINEAGE.set(("default-main", "ops-main"))
    try:
        with pytest.raises(PermissionError, match="loop"):
            await send(star, "ops-main", "peer")
    finally:
        HANDOFF_LINEAGE.reset(token)


@pytest.mark.asyncio
async def test_registered_task_cannot_bypass_star_via_legacy_tool(star):
    service, _ = star
    service.policy.update(source_profile="default", source_parent_channel="projects",
                          target_profile="aiyu-ops", target_session_key="ops-main")
    # Even a legacy policy that would otherwise permit this route cannot bypass registration.
    with pytest.raises(PermissionError, match="document"):
        await service.submit("default-task", "default-task-sid", service.runner.home / "default",
                             "legacy-new-01", "bypass")


@pytest.mark.asyncio
async def test_reset_source_and_nested_admission_cannot_replay_or_loop(star):
    service, _ = star
    await send(star, "default-task", "main")
    service.runner.entries["default-task"].session_id = "reset"
    await service.tick()
    assert service.journal.get("request-0001")["status"] == "failed"
    assert not service.runner.received
    service.runner.entries["default-task"].session_id = "default-task-sid"
    original = service.runner.handle

    async def nested(event):
        with pytest.raises(PermissionError, match="loop"):
            await send(star, "default-main", "task:task-one", request="nested-00001")
        await original(event)

    service.runner.adapter.handle_message = nested
    await send(star, "default-task", "main", request="request-0002")
    await service.tick()
    assert service.journal.get("request-0002")["status"] == "delivered"
    assert service.journal.get("nested-00001") is None


@pytest.mark.asyncio
async def test_registry_dispatch_requires_native_context_not_environment(star, monkeypatch, isolated_entrypoint_bootstrap):
    import gateway.agent_handoff as module
    import tools.agent_handoff_tool
    from gateway.session_context import _SESSION_ID, _SESSION_KEY
    from tools.registry import registry
    service, path = star
    service.runner._gateway_loop = asyncio.get_running_loop()
    service.runner._served_profile_for_home = lambda h: "default"
    monkeypatch.setattr(module, "_service", service)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: service.runner.home / "default")
    monkeypatch.setenv("HERMES_SESSION_KEY", "default-task")
    monkeypatch.setenv("HERMES_SESSION_ID", "default-task-sid")
    args = {"request_id": "registry-0001", "task_id": "task-one", "handoff_path": str(path), "target": "main"}
    refused = registry.dispatch("coordinate_session", args, session_id="default-task-sid")
    assert "bound native" in json.loads(refused)["error"]
    key = _SESSION_KEY.set("default-task"); sid = _SESSION_ID.set("default-task-sid")
    try:
        from gateway.session_context import native_model_turn_scope
        with native_model_turn_scope(service.runner, service.runner.entries['default-task'].origin,
                                     'default-task', 'default-task-sid'):
            response = await asyncio.to_thread(registry.dispatch, "coordinate_session", args, session_id="default-task-sid")
        assert json.loads(response)["status"] == "queued"
    finally:
        _SESSION_KEY.reset(key); _SESSION_ID.reset(sid)


@pytest.mark.asyncio
async def test_real_native_main_channel_route_and_changed_route_refusal(tmp_path, monkeypatch, isolated_entrypoint_bootstrap):
    from datetime import datetime
    from unittest.mock import AsyncMock
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry
    from tests.gateway.test_multiplex_transport_matrix import _stub

    home = tmp_path / "home"; ops_home = home / "profiles" / "aiyu-ops"; ops_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda **kw: [("default", home), ("aiyu-ops", ops_home)])
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda n: home if n == "default" else ops_home)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda n: True)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True, extra={})}
    route = {"name": "ops", "platform": "discord", "profile": "aiyu-ops", "chat_id": "ops-parent"}
    runner.config.profile_routes = parse_profile_routes([route]); runner._primary_profile_name = "default"
    adapter = _stub(Platform.DISCORD, runner, "shared-discord")
    runner.adapters, runner._profile_adapters = {Platform.DISCORD: adapter}, {"aiyu-ops": {}}
    runner._is_user_authorized_for_source = lambda *a, **kw: True
    entries, mains = {}, {}
    for profile, chat in (("default", "default-main"), ("aiyu-ops", "ops-parent")):
        source = adapter.build_source(chat_id=chat, chat_type="group", user_id="owner", scope_id="guild")
        assert runner._canonicalize(source).runtime_profile == profile
        key = runner._session_key_for_source(source); now = datetime.now()
        entries[key] = SessionEntry(key, profile + "-sid", now, now, origin=source, transport_profile="default")
        persist_peer(runner._resolve_profile_home_for_source(source), key, entries[key])
        mains[profile] = {"session_key": key, "session_id": profile + "-sid", "chat_id": chat}
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store,
        lookup_by_session_key=AsyncMock(side_effect=lambda k: entries.get(k)))
    path = tmp_path / "docs" / "project" / "task-one" / "HANDOFF.md"
    path.parent.mkdir(parents=True); path.write_text("nonce")
    policy = {"enabled": True, "execution_enabled": False, "coordination": {
        "enabled": True, "owner_user_id": "owner", "guild_id": "guild", "handoff_root": str(tmp_path / "docs"),
        "mains": mains, "tasks": {}, "task_parents": {}}}
    service = HandoffService(runner, home, policy)
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_task_source import accept_task
    from hermes_cli import projects_db
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    kb.create_board('registry', tracking_only=True)
    source = SessionSource(Platform.DISCORD, 'task-thread', chat_type='thread', user_id='owner',
                           thread_id='task-thread', parent_chat_id='default-main', scope_id='guild')
    policy['coordination']['task_parents'] = {'default': ['default-main']}
    with projects_db.connect_closing() as conn:
        project_id = projects_db.create_project(conn, name='Project', slug='system', primary_path=str(tmp_path))
    with kbc.connect_closing(board='registry') as conn:
        accept_task(conn, policy=policy['coordination'], source=source, session_key='task-key',
                    session_id='task-sid', profile_home=home, task_id='task-one',
                    document_project='project', project_id=project_id, title='task')
    key = mains["aiyu-ops"]["session_key"]
    reply = await submit_document(service, key, "aiyu-ops-sid", ops_home, "native-00001", "task-one", str(path), "peer")
    assert reply["status"] == "queued"
    runner.config.profile_routes = parse_profile_routes([{**route, "profile": "default"}])
    with pytest.raises(PermissionError, match="canonical route"):
        await service.pinned_routes(service.journal.get("native-00001"))


@pytest.mark.asyncio
async def test_document_directory_symlink_fifo_and_size_fail_closed(star, tmp_path):
    import os
    from gateway.agent_coordination import document_pointer
    service, path = star; policy = service.policy["coordination"]
    path.unlink(); os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        document_pointer(policy, "task-one", str(path))
    path.unlink(); path.write_bytes(b"x" * 262145)
    with pytest.raises(ValueError, match="256 KiB"):
        document_pointer(policy, "task-one", str(path))
    path.unlink(); path.parent.rmdir(); path.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        document_pointer(policy, "task-one", str(path))
    alias = tmp_path / "root-alias"
    alias.symlink_to(policy["handoff_root"], target_is_directory=True)
    with pytest.raises(PermissionError, match="canonical"):
        document_pointer({**policy, "handoff_root": str(alias)}, "task-one", str(alias / "project/task-one/HANDOFF.md"))


@pytest.mark.asyncio
async def test_effective_config_startup_and_missing_main_are_fail_closed(star, monkeypatch):
    import gateway.agent_handoff as module
    from gateway.agent_coordination import coordination_policy
    service, _ = star
    service.policy["execution_enabled"] = False
    home = service.home
    (home / "config.yaml").write_text(json.dumps({"agent_handoff": service.policy}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_constants.get_routing_process_hermes_home", lambda: home)
    monkeypatch.setattr(module, "_service", None)
    module.install_handoff_service(service.runner)
    try:
        loaded = module._service
        from hermes_cli.kanban_task_source import read_sources
        assert coordination_policy(loaded) == dict(service.policy['coordination'], tasks=read_sources(home))
        assert not service.runner.received
        loaded.policy["coordination"]["mains"]["aiyu-ops"]["session_id"] = None
        with pytest.raises(PermissionError, match="explicit main"):
            await send((loaded, star[1]), "default-task", "main")
    finally:
        service.runner._running = False
        await asyncio.gather(*list(service.runner._background_tasks))


@pytest.mark.asyncio
async def test_native_compression_is_allowed_but_reset_is_not(star):
    from hermes_state import SessionDB
    service, _ = star
    for profile, key in (("default", "default-main"), ("aiyu-ops", "ops-main")):
        home = service.runner.home / profile; home.mkdir(exist_ok=True)
        db = SessionDB(home / "state.db")
        db.create_session(key + "-sid", source="discord", model="test")
        db.end_session(key + "-sid", "compression")
        db.create_session(key + "-tip", source="discord", model="test", parent_session_id=key + "-sid")
        db.close()
    await send(star, "default-main", "peer")
    service.runner.entries["default-main"].session_id = "default-main-tip"
    service.runner.entries["ops-main"].session_id = "ops-main-tip"
    await service.tick()
    assert service.journal.get("request-0001")["status"] == "delivered"
    event, _ = service.runner.received
    assert event.metadata["gateway_session_id"] == "ops-main-tip"
    await submit_document(service, "default-main", "default-main-tip", service.runner.home / "default",
                          "request-0002", "task-one", str(star[1]), "peer")
    service.runner.entries["ops-main"].session_id = "ops-reset"
    await service.tick()
    assert service.journal.get("request-0002")["status"] == "failed"
    assert len(service.runner.received) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "returning", "generated"])
async def test_document_receipt_restart_does_not_reexecute_or_replay_unknown_work(star, state):
    service, path = star
    await send(star, "default-task", "main")
    # Frozen v1 receipts retain their send-only return semantics across the upgrade.
    service.journal.update("request-0001", protocol=None, status=state, response="persisted native final")
    path.unlink()
    restarted = HandoffService(service.runner, service.home, service.policy)
    await restarted.tick()
    assert not service.runner.received
    receipt = restarted.journal.get("request-0001")
    if state == "generated":
        assert receipt["status"] == "delivered"
        assert "persisted native final" in service.runner.sent[0][1]
        assert receipt["document"]["sha256"] in service.runner.sent[0][1]
    else:
        assert receipt["status"] == "ambiguous" and not service.runner.sent


@pytest.mark.asyncio
async def test_legacy_pending_and_receipts_keep_original_destination_after_migration(star):
    service, _ = star
    service.policy.update(source_profile="default", source_parent_channel="projects",
                          target_profile="aiyu-ops", target_session_key="ops-main")
    service.policy["coordination"]["enabled"] = False
    await service.submit("default-task", "default-task-sid", service.runner.home / "default", "legacy-old-01", "ping")
    service.policy["coordination"]["enabled"] = True
    await service.tick()
    assert service.runner.received[0].source.chat_id == "ops-main"
    receipt = await service.submit("default-task", "default-task-sid", service.runner.home / "default", "legacy-old-01", "ping")
    assert receipt["status"] == "delivered"
    with pytest.raises(PermissionError, match="document"):
        await service.submit("default-task", "default-task-sid", service.runner.home / "default", "legacy-new-02", "ping")
