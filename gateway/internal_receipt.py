"""Process-local receipts for native internal turns; never deserialized from metadata."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from gateway.platforms.event import ProcessingOutcome


@dataclass
class InternalTurnReceipt:
    future: asyncio.Future
    author: dict
    response: str = ""
    generated: bool = False
    interrupted: bool = False
    message_ids: list[str] = field(default_factory=list)


def receipt_for(event):
    receipt = getattr(event, "_internal_turn_receipt", None)
    return receipt if event is not None and event.internal and isinstance(receipt, InternalTurnReceipt) else None


def capture_internal_response(event, response: str, *, successful: bool) -> None:
    receipt = receipt_for(event)
    if receipt is not None:
        receipt.response = str(response or "")
        receipt.generated = successful and bool(receipt.response) and not receipt.interrupted


def finish_internal_receipt(event, outcome: ProcessingOutcome) -> None:
    receipt = receipt_for(event)
    if receipt is not None and not receipt.future.done():
        receipt.future.set_result({"response": receipt.response, "generated": receipt.generated,
                                   "delivered": outcome == ProcessingOutcome.SUCCESS and not receipt.interrupted,
                                   "message_ids": list(receipt.message_ids)})


def capture_internal_delivery(event, result):
    receipt = receipt_for(event)
    message_id = str(getattr(result, "message_id", "") or "")
    if receipt is not None and getattr(result, "success", False) is True and message_id:
        if message_id not in receipt.message_ids:
            receipt.message_ids.append(message_id)


def internal_turn_metadata(event):
    receipt = receipt_for(event)
    return {"native_handoff_author": receipt.author} if receipt is not None else {}


def invalidate_internal_receipt(event):
    receipt = receipt_for(event) if event is not None else None
    if receipt is not None:
        receipt.interrupted, receipt.generated = True, False


def invalidate_active_receipt(runner, key, result, pending_event, pending):
    if pending_event or pending or (result and result.get("interrupted")):
        state = runner._peek_session_state(key)
        invalidate_internal_receipt(state.turn.event if state else None)


def promote_owned_internal_overflow(adapter, event, key, guard):
    """An early-rejected wake/FIFO head never reaches runner drain; its owner must hand off.

    No await separates ownership validation, FIFO promotion and Base's existing cleanup.
    A replacement task/guard or shutdown retains its own queue disposal policy.
    """
    from gateway.fifo_handoff import owns_fifo_dispatch, stage_fifo_head
    runner = adapter.gateway_runner
    owner = asyncio.current_task()
    if ((not event.internal and not owns_fifo_dispatch(adapter, event, key, guard))
            or runner is None or runner._draining or owner is None or guard is None
            or adapter._session_tasks.get(key) is not owner
            or adapter._active_sessions.get(key) is not guard
            or key in adapter._pending_messages):
        return
    pending = runner._promote_queued_event(key, adapter, None)
    if pending is not None:
        stage_fifo_head(runner, adapter, key, pending)


async def drain_pending_turn(runner, result, adapter, source, key):
    """Let an owned native final settle before the adapter drains unrelated queued work.

    The recursive lane rebuilds the delivery event and returns the last turn's
    result. Neither can acknowledge the private receipt of the turn it displaced.
    Human input and gateway drain retain their existing promotion/discard path.
    Internal notifications skip ingress orphan rescue. Base's owned cleanup must
    promote overflow if a notification exits before runner drain. An adapter stop
    invalidates this receipt while preserving its parked notification.
    """
    state = runner._peek_session_state(key)
    receipt = receipt_for(state.turn.event if state else None)
    pending = adapter._pending_messages.get(key) if adapter is not None else None
    if (receipt is not None and not receipt.future.done() and result
            and not result.get("interrupted") and not (result.get("pending_steer") or "").strip()
            and not runner._draining and getattr(pending, "internal", False)
            and receipt_for(pending) is None):
        guard = adapter._active_sessions.get(key)
        if guard is not None and guard.is_set():
            invalidate_internal_receipt(state.turn.event)
        return None, None
    return await runner._run_agent_drain_pending(result, adapter, source, key)


def turn_author_for(ctx, fallback=None):
    """Keep native handoff attribution distinct from the human who owns its delivery route."""
    return (ctx.persist_user_display_metadata or {}).get("native_handoff_author") or fallback or {
        "id": ctx.source.user_id or None, "name": ctx.source.user_name or None,
        "is_bot": bool(getattr(ctx.source, "is_bot", False)),
    }
