"""Plugin-registered Feishu card button handlers (``hermes_plugin_action``)."""

from unittest.mock import MagicMock, patch

import pytest

from tests.gateway.test_feishu_approval_buttons import _make_adapter, _make_card_action_data
from plugins.platforms.feishu import card_actions


@pytest.fixture(autouse=True)
def _clean_handlers():
    card_actions._handlers.clear()
    yield
    card_actions._handlers.clear()


def test_register_validates_and_latest_wins():
    with pytest.raises(ValueError):
        card_actions.register_card_action_handler("", lambda o, v: None)
    with pytest.raises(ValueError):
        card_actions.register_card_action_handler("k", "not callable")
    first, second = (lambda o, v: None), (lambda o, v: None)
    card_actions.register_card_action_handler("k", first)
    card_actions.register_card_action_handler("k", second)
    assert card_actions._handlers == {"k": second}


def _click(value, open_id="ou_user1"):
    adapter = _make_adapter()
    adapter._loop = MagicMock(is_closed=lambda: False)
    adapter._card_response = lambda card=None: {"card": card}
    with patch.object(adapter, "_submit_on_loop") as submit:
        result = adapter._on_card_action_trigger(_make_card_action_data(value, open_id=open_id))
    return result, submit


def test_click_runs_handler_inline_and_never_reaches_an_agent():
    seen = []

    def handler(open_id, value):
        seen.append((open_id, value))
        return {"elements": [{"tag": "markdown", "content": "已选：行"}]}

    card_actions.register_card_action_handler("daily", handler)
    result, submit = _click({"hermes_plugin_action": "daily", "pick": "ok"})
    assert seen == [("ou_user1", {"hermes_plugin_action": "daily", "pick": "ok"})]
    assert result == {"card": {"elements": [{"tag": "markdown", "content": "已选：行"}]}}
    submit.assert_not_called()


def test_unknown_key_and_handler_error_are_dropped_without_routing():
    def boom(open_id, value):
        raise RuntimeError("x")

    card_actions.register_card_action_handler("boom", boom)
    for key in ("missing", "boom"):
        result, submit = _click({"hermes_plugin_action": key})
        assert result == {"card": None}
        submit.assert_not_called()
