"""``approval`` blocks: a human gate that no agent path can release.

Only :func:`kanban_db.approve_task` (called by the gateway for a configured
approver, the interactive CLI, or the dashboard) releases it. unblock /
complete / archive / promote / unlink and auto-promotion are all refused.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _gate_with_child(conn):
    """A parked approval gate (no assignee) with a deploy child behind it."""
    gate = kb.create_task(conn, title="approve deploy", initial_status="blocked")
    assert kb.block_task(conn, gate, kind="approval", reason="needs owner sign-off")
    child = kb.create_task(conn, title="deploy", assignee="ops", parents=[gate])
    return gate, child


def _status(conn, tid):
    return kb.get_task(conn, tid).status


def test_gate_is_awaiting_approval_and_child_waits(kanban_home):
    with kbc.connect_closing() as conn:
        gate, child = _gate_with_child(conn)
        assert kb.is_awaiting_approval(conn, gate)
        kb.recompute_ready(conn)
        assert _status(conn, gate) == "blocked"
        assert _status(conn, child) == "todo"


@pytest.mark.parametrize("op", [
    lambda conn, g, c: kb.unblock_task(conn, g),
    lambda conn, g, c: kb.complete_task(conn, g, result="ok", force=True),
    lambda conn, g, c: kb.archive_task(conn, g),
    lambda conn, g, c: kb.unlink_tasks(conn, g, c),
], ids=["unblock", "complete", "archive", "unlink"])
def test_agent_paths_cannot_release_gate(kanban_home, op):
    with kbc.connect_closing() as conn:
        gate, child = _gate_with_child(conn)
        with pytest.raises(kb.ApprovalRequiredError):
            op(conn, gate, child)
        assert kb.is_awaiting_approval(conn, gate)
        assert _status(conn, child) == "todo"


def test_promote_refused(kanban_home):
    with kbc.connect_closing() as conn:
        gate, _ = _gate_with_child(conn)
        ok, reason = kb.promote_task(conn, gate, actor="router")
        assert not ok and "approval" in reason
        assert kb.is_awaiting_approval(conn, gate)


def test_approve_completes_gate_and_releases_child(kanban_home):
    with kbc.connect_closing() as conn:
        gate, child = _gate_with_child(conn)
        assert kb.approve_task(conn, gate, approver="slack:U1")
        assert _status(conn, gate) == "done"
        assert _status(conn, child) == "ready"
        events = [e for e in kb.list_events(conn, gate) if e.kind == "approved"]
        assert events and events[-1].payload["approver"] == "slack:U1"
        # Second approval is a no-op, not an error.
        assert kb.approve_task(conn, gate, approver="slack:U1") is False


def test_approve_refused_until_parents_done(kanban_home):
    with kbc.connect_closing() as conn:
        plan = kb.create_task(conn, title="plan", assignee="ops")
        gate = kb.create_task(conn, title="approve", initial_status="blocked", parents=[plan])
        assert kb.block_task(conn, gate, kind="approval")
        with pytest.raises(ValueError, match="cannot be approved yet"):
            kb.approve_task(conn, gate, approver="slack:U1")
        assert not [e for e in kb.list_events(conn, gate) if e.kind == "approved"]
        assert kb.complete_task(conn, plan, result="plan ready", force=True)
        assert kb.approve_task(conn, gate, approver="slack:U1")
        assert _status(conn, gate) == "done"


def test_approve_requires_approver(kanban_home):
    with kbc.connect_closing() as conn:
        gate, _ = _gate_with_child(conn)
        with pytest.raises(ValueError):
            kb.approve_task(conn, gate, approver=" ")


def test_worker_approval_block_resumes_its_worker(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rotate key", assignee="ops")
        assert kb.claim_task(conn, tid, claimer="ops") is not None
        assert kb.block_task(conn, tid, kind="approval", reason="rotate prod key?")
        assert kb.is_awaiting_approval(conn, tid)
        with pytest.raises(kb.ApprovalRequiredError):
            kb.unblock_task(conn, tid)
        assert kb.approve_task(conn, tid, approver="cli:owner")
        assert _status(conn, tid) == "ready"


def test_repeated_approval_blocks_never_route_to_triage(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="step", assignee="ops")
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
            assert kb.claim_task(conn, tid, claimer="ops") is not None
            assert kb.block_task(conn, tid, kind="approval")
            assert _status(conn, tid) == "blocked"
            assert kb.approve_task(conn, tid, approver="cli:owner")


def test_cli_approve_refuses_without_terminal(kanban_home):
    with kbc.connect_closing() as conn:
        gate, _ = _gate_with_child(conn)
    out = kanban_cli.run_slash(f"approve {gate}")
    assert "interactive terminal" in out
    out = kanban_cli.run_slash(f"unblock {gate}")
    assert "waiting for human approval" in out
    with kbc.connect_closing() as conn:
        assert kb.is_awaiting_approval(conn, gate)


def _event(user_id, *, is_bot=False, internal=False, relay=False):
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), user_id=user_id,
                             is_bot=is_bot, delivered_via_upstream_relay=relay)
    return SimpleNamespace(source=source, internal=internal, allow_gateway_control=True)


@pytest.mark.parametrize("event,approved", [
    (_event("UOWNER"), True),
    (_event("UCOLLEAGUE"), False),
    (_event("UOWNER", is_bot=True), False),
    (_event("UOWNER", internal=True), False),
    (_event("UOWNER", relay=True), False),
], ids=["approver", "colleague", "bot", "internal", "relay"])
def test_gateway_chat_approve_checks_sender(kanban_home, monkeypatch, event, approved):
    from gateway import slash_commands
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"approvers": ["slack:UOWNER"]}})
    with kbc.connect_closing() as conn:
        gate, child = _gate_with_child(conn)
    reply = slash_commands._kanban_chat_approve(event, f"approve {gate}", None)
    with kbc.connect_closing() as conn:
        assert kb.is_awaiting_approval(conn, gate) is (not approved)
        assert _status(conn, child) == ("ready" if approved else "todo")
    assert reply.startswith("✅" if approved else "⛔")
