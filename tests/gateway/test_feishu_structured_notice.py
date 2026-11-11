"""Feishu renders a kanban StructuredNotice as a button-less interactive card."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.i18n import t
from gateway.platforms.base import SendResult
from gateway.structured_notice import StructuredNotice
from tests.gateway.test_feishu_approval_buttons import _make_adapter
from plugins.platforms.feishu.notice_card import _card

_OK = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_card"))


def _notice(kind="completed", tone="success", body="done", decision=None):
    facts = ((t("gateway.kanban.card.fact_task"), "t_abc123"), (t("gateway.kanban.card.fact_board"), "main"))
    return StructuredNotice(kind=kind, tone=tone, title="Done: ship it", body_markdown=body,
                            decision_markdown=decision, facts=facts, fallback_text="plain ping")


async def _send(adapter, notice):
    with patch.object(adapter, "_feishu_send_with_retry", new_callable=AsyncMock, return_value=_OK) as mock_send:
        result = await adapter.send_structured_notice("oc_1", notice, None)
    assert result.success is True and result.message_id == "om_card"
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["msg_type"] == "interactive"
    return json.loads(mock_send.call_args.kwargs["payload"])


def _markdown(card):
    return [el["content"] for el in card["elements"] if el["tag"] == "markdown"]


@pytest.mark.asyncio
async def test_completed_notice_is_green_card_with_facts_and_sections():
    body = "## Changes\n- added notice card\n\n## Tests\n- all green"
    card = await _send(_make_adapter(), _notice(body=body))
    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "Done: ship it"
    blocks = _markdown(card)
    assert "t_abc123" in blocks[0] and "main" in blocks[0]
    assert len(blocks) == 3  # facts + one block per level-2 heading
    assert blocks[1].startswith("**Changes**") and blocks[2].startswith("**Tests**")
    assert [el["tag"] for el in card["elements"]] == ["markdown", "hr", "markdown", "hr", "markdown"]


@pytest.mark.asyncio
async def test_blocked_notice_is_orange_and_ends_with_decision_block():
    card = await _send(_make_adapter(), _notice(kind="blocked", tone="warning", body="needs a key",
                                                 decision="Run `hermes kanban unblock t_abc123`."))
    assert card["header"]["template"] == "orange"
    last = card["elements"][-1]
    assert last["tag"] == "markdown"
    assert last["content"].startswith(f"**{t('gateway.kanban.card.decision_label')}**")
    assert "hermes kanban unblock t_abc123" in last["content"]


@pytest.mark.asyncio
async def test_long_body_is_truncated_and_full_report_sent_as_md_file():
    body = ("x" * 498 + "\n\n") * 6 + "x" * 500
    assert len(body) == 3500
    adapter = _make_adapter()
    sent = {}

    def _fake_document(chat_id, file_path, caption=None, file_name=None, **kwargs):
        with open(file_path, encoding="utf-8") as fh:
            sent.update(path=file_path, name=file_name, text=fh.read())
        return SendResult(success=True, message_id="om_file")

    with patch.object(adapter, "send_document", new_callable=AsyncMock, side_effect=_fake_document) as mock_doc:
        card = await _send(adapter, _notice(body=body))

    mock_doc.assert_called_once()
    note = t("gateway.kanban.card.truncated_note")
    blocks = _markdown(card)
    body_blocks = [b for b in blocks[1:] if b != note]
    assert note in blocks
    assert sum(len(b) for b in body_blocks) <= 3000
    assert sent["name"].startswith("kanban-") and sent["name"] == "kanban-t_abc123-completed.md"
    assert sent["text"] == body
    assert not os.path.exists(sent["path"])
    assert not os.path.exists(os.path.dirname(sent["path"]))


@pytest.mark.asyncio
async def test_attachment_failure_does_not_fail_the_card():
    adapter = _make_adapter()
    with patch.object(adapter, "send_document", new_callable=AsyncMock,
                      return_value=SendResult(success=False, error="upload failed")):
        card = await _send(adapter, _notice(body="y" * 3500))
    assert t("gateway.kanban.card.truncated_note") in _markdown(card)


@pytest.mark.asyncio
async def test_notice_card_and_plain_card_have_no_action_row_without_actions():
    card = await _send(_make_adapter(), _notice(kind="gave_up", tone="danger", body="kept failing",
                                                 decision="Fix and unblock."))
    assert card["header"]["template"] == "red"
    assert all(el["tag"] != "action" for el in card["elements"])
    assert all(el["tag"] != "action" for el in _card("title", "blue", "text")["elements"])


@pytest.mark.asyncio
async def test_not_connected_returns_failure_without_sending():
    adapter = _make_adapter()
    adapter._client = None
    with patch.object(adapter, "_feishu_send_with_retry", new_callable=AsyncMock) as mock_send:
        result = await adapter.send_structured_notice("oc_1", _notice(), None)
    assert result.success is False
    mock_send.assert_not_called()


def test_body_splits_into_at_most_six_sections_and_ignores_fenced_headings():
    from plugins.platforms.feishu.notice_card import build_notice_card
    body = "intro\n" + "".join(f"# H{i}\ntext {i}\n" for i in range(8)) + "```\n# not a heading\n```"
    card, overflow = build_notice_card(_notice(body=body))
    blocks = _markdown(card)[1:]
    assert overflow is None
    assert len(blocks) == 6
    assert blocks[0] == "intro" and blocks[-1].startswith("**H4**") and "**H7**" in blocks[-1]
    assert "# not a heading" in blocks[-1]


@pytest.mark.asyncio
async def test_unwritable_report_file_does_not_fail_the_card():
    adapter = _make_adapter()
    with patch("plugins.platforms.feishu.notice_card.tempfile.mkdtemp", side_effect=OSError("disk full")), \
            patch.object(adapter, "send_document", new_callable=AsyncMock) as mock_doc:
        await _send(adapter, _notice(body="z" * 3500))
    mock_doc.assert_not_called()
