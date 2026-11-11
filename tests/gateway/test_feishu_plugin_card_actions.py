"""Plugin-registered Feishu card button handlers (``hermes_plugin_action``)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.gateway.test_feishu_approval_buttons import _make_adapter, _make_card_action_data
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _make_ctx():
    mgr = PluginManager()
    manifest = PluginManifest(name="test_plugin", version="0.0.1", description="t", source="user",
                              path=Path("/tmp/test_plugin"))
    return mgr, PluginContext(manifest=manifest, manager=mgr)


def test_register_validates_and_latest_wins():
    mgr, ctx = _make_ctx()
    with pytest.raises(ValueError):
        ctx.register_feishu_card_action_handler("", lambda o, v: None)
    with pytest.raises(ValueError):
        ctx.register_feishu_card_action_handler("k", "not callable")
    first, second = (lambda o, v: None), (lambda o, v: None)
    ctx.register_feishu_card_action_handler("k", first)
    ctx.register_feishu_card_action_handler("k", second)
    assert mgr.get_feishu_card_action_handler("k") == (second, "test_plugin")
    assert mgr.get_feishu_card_action_handler("other") is None


def _click(adapter, mgr, value, open_id="ou_user1"):
    adapter._loop = MagicMock(is_closed=lambda: False)
    adapter._card_response = lambda card=None: {"card": card}
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=mgr), \
         patch.object(adapter, "_submit_on_loop") as submit:
        result = adapter._on_card_action_trigger(_make_card_action_data(value, open_id=open_id))
    return result, submit


def test_click_runs_handler_inline_and_never_reaches_an_agent():
    mgr, ctx = _make_ctx()
    seen = []

    def handler(open_id, value):
        seen.append((open_id, value))
        return {"elements": [{"tag": "markdown", "content": "已选：行"}]}

    ctx.register_feishu_card_action_handler("daily", handler)
    result, submit = _click(_make_adapter(), mgr, {"hermes_plugin_action": "daily", "pick": "ok"})
    assert seen == [("ou_user1", {"hermes_plugin_action": "daily", "pick": "ok"})]
    assert result == {"card": {"elements": [{"tag": "markdown", "content": "已选：行"}]}}
    submit.assert_not_called()


def test_unknown_key_and_handler_error_are_dropped_without_routing():
    mgr, ctx = _make_ctx()

    def boom(open_id, value):
        raise RuntimeError("x")

    ctx.register_feishu_card_action_handler("boom", boom)
    for key in ("missing", "boom"):
        result, submit = _click(_make_adapter(), mgr, {"hermes_plugin_action": key})
        assert result == {"card": None}
        submit.assert_not_called()
