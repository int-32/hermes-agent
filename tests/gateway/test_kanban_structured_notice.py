"""Kanban outcome pings offer a structured notice to adapters that opt in and fall back to text."""
import asyncio

import pytest

from agent import i18n
from agent.i18n import t
from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.platforms.base import SendResult
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tests.gateway import test_kanban_wake_acceptance as wake_acceptance

SUMMARY = "## Result\n\nAll green.\n\n## Next\n\n" + "x" * 900


class NoticeAdapter(WireAdapter):
    """Records structured notices; ``refuse`` makes it report SendResult(success=False)."""

    refuse = False

    async def send_structured_notice(self, chat_id, notice, metadata):
        self.notices.append(notice)
        return SendResult(success=not self.refuse, error="bad card payload" if self.refuse else None)


class RefusingNoticeAdapter(NoticeAdapter):
    refuse = True


def _route(monkeypatch, adapter_cls=WireAdapter):
    monkeypatch.setattr(wake_acceptance, "WireAdapter", adapter_cls)
    runner, adapter, _source, key = wake_acceptance.setup_route()
    adapter.notices = []
    return runner, adapter, key


def _task(key, mode, outcome):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="ship it", assignee="worker", session_id=key)
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="42",
                           user_id="42", chat_type="dm", delivery_mode=mode)
        outcome(conn, tid)
        return tid
    finally:
        conn.close()


def _complete(conn, tid):
    kb.complete_task(conn, tid, summary=SUMMARY)


def _block(conn, tid):
    kb.block_task(conn, tid, reason="need the staging credential", kind="needs_input")


def _crash(conn, tid):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "crashed")


async def _tick(runner, failures):
    deliveries = await asyncio.to_thread(_notifier_collect, runner, kb,
        notifier_profile=None, gc_due=False, gc_retention_days=30)
    for delivery in deliveries:
        await _KanbanNotification(runner, delivery, platform_cls=Platform, sub_fail_counts=failures).deliver()


def _sub_row(tid):
    conn = kbc.connect()
    try:
        row = conn.execute("SELECT last_ping_event_id, last_event_id FROM kanban_notify_subs WHERE task_id=?",
                           (tid,)).fetchone()
        unseen = kbn.unseen_events_for_sub(conn, task_id=tid, platform="telegram", chat_id="42",
                                           kinds=["completed", "blocked", "crashed"])[1]
        return dict(row), unseen
    finally:
        conn.close()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setattr("gateway.mirror.mirror_to_session", lambda *a, **k: True)
    # The expected ping texts below are the English catalog's.
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    i18n.reset_language_cache()
    yield
    i18n.reset_language_cache()


@pytest.mark.asyncio
async def test_adapter_without_notice_method_gets_unchanged_text(board, monkeypatch):
    runner, adapter, key = _route(monkeypatch)
    direct = _task(key, "direct", _complete)
    done = _task(key, "notify", _complete)
    blocked = _task(key, "notify", _block)
    await adapter.connect()
    try:
        await _tick(runner, {})
    finally:
        await adapter.disconnect()
    assert sorted(adapter.wire) == sorted([
        SUMMARY,
        f"✔ [default] @worker Kanban {done} done — ship it\n## Result",
        f"⏸ [default] @worker Kanban {blocked} blocked: need the staging credential",
    ])
    assert _sub_row(direct)[1] == []


@pytest.mark.asyncio
async def test_notice_adapter_receives_cards_for_outcome_kinds_only(board, monkeypatch):
    runner, adapter, key = _route(monkeypatch, NoticeAdapter)
    direct = _task(key, "direct", _complete)
    blocked = _task(key, "notify", _block)
    _task(key, "notify", _crash)
    await adapter.connect()
    try:
        await _tick(runner, {})
    finally:
        await adapter.disconnect()
    by_task = {n.facts[0][1]: n for n in adapter.notices}
    assert set(by_task) == {direct, blocked}, "crashed stays a plain text ping"
    done = by_task[direct]
    assert (done.kind, done.tone, done.body_markdown, done.fallback_text) == ("completed", "success", SUMMARY, SUMMARY)
    assert done.decision_markdown is None and done.title == t("gateway.kanban.card.title_completed", title="ship it")
    card = by_task[blocked]
    assert card.tone == "warning" and card.decision_markdown
    assert card.body_markdown == "need the staging credential"
    assert card.fallback_text == f"⏸ [default] @worker Kanban {blocked} blocked: need the staging credential"
    assert (t("gateway.kanban.card.fact_assignee"), "@worker") in card.facts
    assert len(adapter.wire) == 1 and adapter.wire[0].startswith("✖ [default] @worker Kanban ")


@pytest.mark.asyncio
async def test_refused_notice_falls_back_to_text_once(board, monkeypatch):
    runner, adapter, key = _route(monkeypatch, RefusingNoticeAdapter)
    tid = _task(key, "notify", _complete)
    failures = {}
    await adapter.connect()
    try:
        await _tick(runner, failures)
        await _tick(runner, failures)
    finally:
        await adapter.disconnect()
    assert len(adapter.notices) == 1
    assert adapter.wire == [adapter.notices[0].fallback_text]
    assert failures == {}
    row, unseen = _sub_row(tid)
    assert unseen == [] and row["last_ping_event_id"] == row["last_event_id"] > 0
