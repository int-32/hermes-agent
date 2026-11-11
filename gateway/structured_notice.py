"""Platform-neutral structured notices for kanban outcome pings.

The kanban notifier builds a :class:`StructuredNotice` for the outcome kinds a
human acts on and offers it to an adapter's optional
``send_structured_notice(chat_id, notice, metadata) -> SendResult``. The
method is deliberately not on ``BasePlatformAdapter``: the notifier probes
``type(adapter)`` for it, and an adapter without it (or one that refuses with
``success=False``) gets ``fallback_text`` through plain ``send()`` — the exact
ping text the notifier sent before notices existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from agent.i18n import t

# Card header colour intent; the adapter maps it to its own palette.
_TONES = {"completed": "success", "blocked": "warning", "block_loop_detected": "warning", "gave_up": "danger"}
_TITLE_KEYS = {
    "completed": "gateway.kanban.card.title_completed",
    "blocked": "gateway.kanban.card.title_blocked",
    "gave_up": "gateway.kanban.card.title_gave_up",
    "block_loop_detected": "gateway.kanban.card.title_triage",
}
_TITLE_LIMIT = 120
_REASON_LIMIT = 600


@dataclass(frozen=True)
class StructuredNotice:
    kind: str  # kanban event kind
    tone: str  # success | warning | danger | info
    title: str  # card header, plain text, <= 120 chars
    body_markdown: str  # full body, never truncated here (adapters own their length budget)
    decision_markdown: Optional[str]  # what the human must do next, rendered as its own block
    facts: tuple[tuple[str, str], ...]  # ordered (localized label, value) pairs
    fallback_text: str  # the plain ping text, sent verbatim by adapters that cannot render a card


def _reason(ev: Any, key: str) -> str:
    from gateway.kanban_watchers_notifier import _safe_review_reason
    value = (ev.payload or {}).get(key)
    return _safe_review_reason(value, _REASON_LIMIT) if value else ""


def _completed_body(ev: Any, n: Any, direct_text: Optional[str]) -> str:
    summary = direct_text or n.d.get("summary") or (ev.payload or {}).get("summary")
    return str(summary or (n.task.result if n.task else "") or "").strip()


def _blocked_parts(ev: Any, n: Any) -> tuple[str, Optional[str]]:
    return _reason(ev, "reason"), t("gateway.kanban.card.decision_unblock", task_id=n.task_id)


def _gave_up_parts(ev: Any, n: Any) -> tuple[str, Optional[str]]:
    failures = (ev.payload or {}).get("failures")
    count = (t("gateway.kanban.ping.failed_n_times", count=int(failures)) if failures
             else t("gateway.kanban.ping.kept_failing"))
    error = _reason(ev, "error")
    last = t("gateway.kanban.ping.last_error", value=error) if error else ""
    return count + last, t("gateway.kanban.card.decision_unblock", task_id=n.task_id)


def _triage_parts(ev: Any, n: Any) -> tuple[str, Optional[str]]:
    payload = ev.payload or {}
    recurrences = payload.get("recurrences")
    body = _reason(ev, "reason")
    if recurrences:
        body += t("gateway.kanban.ping.triage_recurrences", value=str(recurrences)[:200])
    needs_human = payload.get("kind") == "needs_input"
    return body.strip(), t("gateway.kanban.ping.triage_decision" if needs_human else "gateway.kanban.ping.triage_attention")


_PARTS = {"blocked": _blocked_parts, "gave_up": _gave_up_parts, "block_loop_detected": _triage_parts}


def _facts(n: Any) -> tuple[tuple[str, str], ...]:
    facts = [(t("gateway.kanban.card.fact_task"), n.task_id)]
    if n.board_slug:
        facts.append((t("gateway.kanban.card.fact_board"), n.board_slug))
    if n.task and n.task.assignee:
        facts.append((t("gateway.kanban.card.fact_assignee"), f"@{n.task.assignee}"))
    return tuple(facts)


def build_kanban_notice(ev: Any, n: Any, msg: str, direct_text: Optional[str]) -> Optional[StructuredNotice]:
    """The card for ``ev`` on notification ``n``, or None for kinds that stay plain text.

    ``msg`` is the text the notifier would send (the direct reply when there is one) and
    becomes ``fallback_text`` unchanged.
    """
    tone = _TONES.get(ev.kind)
    if tone is None:
        return None
    if ev.kind == "completed":
        body, decision = _completed_body(ev, n, direct_text), None
    else:
        body, decision = _PARTS[ev.kind](ev, n)
    return StructuredNotice(
        kind=ev.kind, tone=tone,
        title=t(_TITLE_KEYS[ev.kind], title=n.title)[:_TITLE_LIMIT],
        body_markdown=body or msg, decision_markdown=decision,
        facts=_facts(n), fallback_text=msg,
    )
