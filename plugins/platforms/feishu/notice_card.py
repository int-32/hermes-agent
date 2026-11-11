"""Feishu interactive-card JSON builders, kept apart from adapter.py (over its size cap).

``build_notice_card`` renders a kanban :class:`~gateway.structured_notice.StructuredNotice` as a
button-less card; ``_card`` / ``_card_button`` are the single-block cards the approval and update
prompts use, plus the approval / update-prompt cards built on them.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from agent.i18n import t

_NOTICE_CARD_BODY_BUDGET = 3000
_NOTICE_CARD_MAX_SECTIONS = 6
_TONE_TEMPLATES = {"success": "green", "warning": "orange", "danger": "red", "info": "blue"}
_HEADING_RE = re.compile(r"^#{1,2}\s+(.+?)\s*#*\s*$")
# choice → catalog key of the resolved-card title; looked up through ``t()`` at resolve time.
_APPROVAL_LABEL_KEYS: dict[str, str] = {
    "once": "platform.feishu.approval.resolved_once", "session": "platform.feishu.approval.resolved_session",
    "always": "platform.feishu.approval.resolved_always", "deny": "platform.feishu.approval.resolved_deny",
}


def _card_button(label: str, btn_type: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": btn_type, "value": value}


def _card_shell(title: str, template: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": title, "tag": "plain_text"}, "template": template},
        "elements": elements,
    }


def _card(title: str, template: str, markdown: str, *, actions: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Wide interactive card: colored header + one markdown block (+ an optional button row)."""
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": markdown}]
    if actions is not None:
        elements.append({"tag": "action", "actions": actions})
    return _card_shell(title, template, elements)


def build_update_prompt_card(*, prompt: str, default: str, prompt_id: int) -> dict[str, Any]:
    default_hint = t("platform.feishu.update_prompt.default_hint", default=default) if default else ""

    def _btn(label: str, answer: str, btn_type: str) -> dict:
        return _card_button(label, btn_type, {"hermes_update_prompt_action": answer, "update_prompt_id": prompt_id})

    actions = [_btn(t("platform.feishu.update_prompt.answer_yes"), "y", "primary"),
               _btn(t("platform.feishu.update_prompt.answer_no"), "n", "danger")]
    return _card(t("platform.feishu.update_prompt.title"), "orange", f"{prompt}{default_hint}", actions=actions)


def build_resolved_approval_card(*, choice: str, user_name: str) -> dict[str, Any]:
    """Raw card JSON shown in place of the buttons once an approval is resolved."""
    icon = "❌" if choice == "deny" else "✅"
    label = t(_APPROVAL_LABEL_KEYS.get(choice, "platform.feishu.approval.resolved_fallback"))
    return _card(f"{icon} {label}", "red" if choice == "deny" else "green",
                 t("platform.feishu.approval.resolved_by_user", icon=icon, label=label, user=user_name))


def build_resolved_update_prompt_card(*, answer: str, user_name: str) -> dict[str, Any]:
    yes = answer == "y"
    title = t("platform.feishu.update_prompt.answered_title", icon="✅" if yes else "❌",
              answer=t("platform.feishu.update_prompt.word_yes" if yes else "platform.feishu.update_prompt.word_no"))
    return _card(title, "green" if yes else "red", t("platform.feishu.update_prompt.answered_by", user=user_name))


def _clip_body(body: str, budget: int) -> tuple[str, Optional[str]]:
    """``body`` cut at its last blank line within ``budget`` chars, plus the full text when cut."""
    if len(body) <= budget:
        return body, None
    head = body[:budget]
    blank = head.rfind("\n\n")
    return (head[:blank] if blank > 0 else head).rstrip(), body


def _body_sections(body: str) -> list[str]:
    """Split at level-1/2 headings (outside code fences), at most six sections.

    Headings become bold lines: the card ``markdown`` element does not render ``#`` headings.
    """
    sections: list[list[str]] = [[]]
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        heading = None if fenced else _HEADING_RE.match(line)
        if heading and any(s.strip() for s in sections[-1]):
            sections.append([])
        sections[-1].append(f"**{heading.group(1)}**" if heading else line)
    texts = [text for text in ("\n".join(s).strip() for s in sections) if text]
    keep = _NOTICE_CARD_MAX_SECTIONS - 1
    return texts[:keep] + ["\n\n".join(texts[keep:])] if len(texts) > _NOTICE_CARD_MAX_SECTIONS else texts


def build_notice_card(notice: Any, *, body_budget: int = _NOTICE_CARD_BODY_BUDGET) -> tuple[dict[str, Any], Optional[str]]:
    """The card JSON for ``notice`` and the full body when the card had to truncate it (else None).

    Layout: facts → body sections → truncation note → "what you need to do", ``hr`` between blocks.
    """
    body, overflow = _clip_body(notice.body_markdown.strip(), body_budget)
    blocks = ["\n".join(f"**{label}**: {value}" for label, value in notice.facts)] if notice.facts else []
    blocks += _body_sections(body)
    if overflow is not None:
        blocks.append(t("gateway.kanban.card.truncated_note"))
    if notice.decision_markdown:
        blocks.append(f"**{t('gateway.kanban.card.decision_label')}**\n{notice.decision_markdown}")
    elements: list[dict[str, Any]] = []
    for block in blocks:
        elements += ([{"tag": "hr"}] if elements else []) + [{"tag": "markdown", "content": block}]
    return _card_shell(notice.title, _TONE_TEMPLATES.get(notice.tone, "blue"), elements), overflow


@contextmanager
def notice_report_file(notice: Any, text: str) -> Iterator[tuple[str, str]]:
    """``(path, file_name)`` of a temp ``kanban-<task_id>-<kind>.md`` holding ``text``; removed on exit."""
    task_id = re.sub(r"[^\w.-]", "_", notice.facts[0][1] if notice.facts else "task")
    file_name = f"kanban-{task_id}-{notice.kind}.md"
    tmp_dir = tempfile.mkdtemp(prefix="hermes-feishu-notice-")
    path = os.path.join(tmp_dir, file_name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        yield path, file_name
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
