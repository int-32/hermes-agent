"""Plugin handlers for Feishu card button clicks.

A button whose value carries ``{"hermes_plugin_action": key, ...}`` is routed to the callback
registered for ``key``. The callback runs synchronously in the SDK callback, so it must be fast
(no model calls); a dict it returns replaces the clicked card inline. The click is consumed
here and never reaches an agent.
"""

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_handlers: dict[str, Callable[[str, dict], Optional[dict]]] = {}


def register_card_action_handler(key: str, callback: Callable[[str, dict], Optional[dict]]) -> None:
    """Register ``callback(open_id, value)`` for ``key``; a later registration replaces an earlier one."""
    key = key.strip() if isinstance(key, str) else ""
    if not key:
        raise ValueError("Feishu card action handler needs a non-empty key")
    if not callable(callback):
        raise ValueError("Feishu card action handler callback must be callable")
    _handlers[key] = callback


def handle_plugin_card_action(event: Any, action_value: dict) -> Optional[dict]:
    """Run the handler for this click; return its replacement card, or None to leave the card."""
    key = str(action_value.get("hermes_plugin_action") or "")
    callback = _handlers.get(key)
    if callback is None:
        logger.warning("[Feishu] No plugin handler for card action %r, dropping click", key)
        return None
    open_id = str(getattr(getattr(event, "operator", None), "open_id", "") or "")
    try:
        card = callback(open_id, dict(action_value))
    except Exception:  # plugin boundary: a broken handler must not break the SDK callback
        logger.exception("[Feishu] Plugin card action handler %r raised", key)
        return None
    return card if isinstance(card, dict) else None
