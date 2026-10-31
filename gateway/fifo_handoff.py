"""One-use, process-local FIFO head transfer; confers no admission authority."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class _QueuedHead:
    runner: object
    adapter: object
    event: object
    key: str
    guard: object


@dataclass(repr=False)
class _Dispatch:
    head: _QueuedHead
    owner: object
    generation: object
    claimed: bool = False
    consumed: bool = False


_DISPATCH = ContextVar("gateway_fifo_dispatch", default=None)


def _generation(head):
    return (getattr(head.guard, "_hermes_run_generation", None),
            head.runner._current_session_run_generation(head.key))


def discard_fifo_head(event):
    if event is not None:
        event.__dict__.pop("_gateway_fifo_head", None)
    return event


def stage_fifo_head(runner, adapter, key, event):
    """Called only when the FIFO producer selects an event for the next-up slot."""
    event._gateway_fifo_head = _QueuedHead(
        runner, adapter, event, key, adapter._active_sessions.get(key))
    adapter._pending_messages[key] = event


def _owned(dispatch, *, generation=True):
    if type(dispatch) is not _Dispatch:
        return False
    head = dispatch.head
    return (head.guard is not None and dispatch.owner is asyncio.current_task()
            and head.adapter.gateway_runner is head.runner
            and head.adapter._session_tasks.get(head.key) is dispatch.owner
            and head.adapter._active_sessions.get(head.key) is head.guard
            and (not generation or _generation(head) == dispatch.generation))


@contextmanager
def fifo_dispatch_scope(adapter, event, key, guard):
    """Base consumes a producer ticket after the native drain task acquires ownership."""
    head = event.__dict__.pop("_gateway_fifo_head", None)
    dispatch = None
    if (type(head) is _QueuedHead and head.adapter is adapter and head.event is event
            and head.key == key and head.guard is guard):
        candidate = _Dispatch(head, asyncio.current_task(), _generation(head))
        if _owned(candidate):
            dispatch = candidate
    token = _DISPATCH.set(dispatch)
    try:
        yield
    finally:
        _DISPATCH.reset(token)


def claim_fifo_head(runner, event):
    """Claim at handler entry, before admission may rewrite the original event."""
    dispatch = _DISPATCH.get()
    if (_owned(dispatch) and dispatch.head.runner is runner and dispatch.head.event is event
            and not dispatch.claimed):
        dispatch.claimed = True
        return dispatch
    return None


def consume_fifo_head(runner, adapter, key, dispatch):
    """After admission, skip exactly one fresh-only rescue in the same session/generation."""
    if (dispatch is _DISPATCH.get() and _owned(dispatch) and dispatch.head.runner is runner
            and dispatch.head.adapter is adapter
            and dispatch.head.key == key and dispatch.claimed and not dispatch.consumed):
        dispatch.consumed = True
        return True
    return False


def owns_fifo_dispatch(adapter, event, key, guard):
    """Cleanup authority stays with this Base task, even after its own turn advances generation."""
    dispatch = _DISPATCH.get()
    return (_owned(dispatch, generation=False) and dispatch.head.adapter is adapter
            and dispatch.head.event is event and dispatch.head.key == key and dispatch.head.guard is guard)
