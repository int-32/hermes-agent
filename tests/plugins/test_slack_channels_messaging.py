"""Messaging contracts through real slack-channels plugin discovery and registry handlers."""

import json

import pytest

from tests.plugins import test_slack_channels as base

FakeApiError = base.FakeApiError
SESSION = base.SESSION
session = base.session
slack = base.slack
channel_tools = base.tools
stub = base.stub

POST = "slack_post_message"
LIST = "slack_list_agents"


@pytest.fixture
def tools(channel_tools):
    from tools.registry import registry

    return {name: registry.get_entry(name) for name in (POST, LIST)}


def run(tools, tool=POST, **args):
    return json.loads(tools[tool].handler(args))


@pytest.fixture
def posted(slack):
    slack.script = {
        "chat.postMessage": {"ok": True, "channel": "C0SESSION", "ts": "3000.000100"},
        "chat.getPermalink": {"ok": True, "permalink": "https://slack/p3000"},
    }
    return slack


def test_post_defaults_to_slack_session_thread(tools, posted, session):
    session(**SESSION)
    result = run(tools, text="hello")
    assert posted.params("chat.postMessage") == {
        "channel": "C0SESSION", "thread_ts": "2000.000100", "text": "hello",
    }
    assert result == {
        "ok": True, "channel_id": "C0SESSION", "ts": "3000.000100",
        "thread_ts": "2000.000100", "permalink": "https://slack/p3000",
    }
    assert posted.params("chat.getPermalink") == {"channel": "C0SESSION", "message_ts": "3000.000100"}


def test_post_defaults_to_trigger_message_without_thread(tools, posted, session):
    session(**{**SESSION, "thread_id": ""})
    run(tools, text="hello")
    assert posted.params("chat.postMessage")["thread_ts"] == SESSION["message_id"]


@pytest.mark.parametrize("thread, expected", [("42.000100", "42.000100"), ("none", None)])
def test_explicit_thread_and_top_level_override_session(tools, posted, session, thread, expected):
    session(**SESSION)
    result = run(tools, text="hello", channel_id="G012345678", thread_ts=thread)
    params = posted.params("chat.postMessage")
    assert params["channel"] == "G012345678"
    assert params.get("thread_ts") == expected
    assert ("thread_ts" in params) is (expected is not None)
    assert result["thread_ts"] == expected


def test_mentions_prefix_message_in_order(tools, posted, session):
    session(**SESSION)
    run(tools, text="Please reply here", mention_user_ids=["U012345678", "U0ABCDEFGH"])
    assert posted.params("chat.postMessage")["text"] == "<@U012345678> <@U0ABCDEFGH> Please reply here"


@pytest.mark.parametrize("mentions", [["U012345678", "bad"], ["U012"], ["U01234567a"], ["U012345678\n"], [1], "U012345678", None])
def test_invalid_mentions_do_not_send(tools, slack, session, mentions):
    session(**SESSION)
    result = run(tools, text="hello", mention_user_ids=mentions)
    assert result["error"] == "invalid_arguments"
    assert slack.calls == []


@pytest.mark.parametrize("target", ["U012345678", "D012345678", "#general", "X012345678"])
def test_only_channel_targets_are_accepted(tools, slack, target):
    result = run(tools, text="hello", channel_id=target, thread_ts="none")
    assert result["error"] == "invalid_target"
    if target.startswith(("U", "D")):
        assert "bot" in result["message"] and "DM" in result["message"]
    assert slack.calls == []


@pytest.mark.parametrize("target", ["U012345678", "D012345678"])
def test_dm_target_rejected_even_without_thread_context(tools, slack, session, target):
    result = run(tools, text="hello", channel_id=target)
    assert result["error"] == "invalid_target"
    assert slack.calls == []


@pytest.mark.parametrize("args, values", [
    ({}, {**SESSION, "platform": "telegram"}),
    ({"channel_id": "C012345678"}, {**SESSION, "platform": "telegram"}),
    ({}, {**SESSION, "chat_id": ""}),
    ({}, {**SESSION, "thread_id": "", "message_id": ""}),
    ({"thread_ts": ""}, SESSION),
    ({"thread_ts": None}, SESSION),
])
def test_no_accidental_top_level_or_cross_platform_defaults(tools, slack, session, args, values):
    session(**values)
    result = run(tools, text="hello", **args)
    assert result["ok"] is False and result["message"] and result["action"]
    assert slack.calls == []


def test_fully_explicit_post_works_without_slack_context(tools, posted, session):
    session(platform="telegram", chat_id="123", thread_id="456")
    assert run(tools, text="hello", channel_id="C012345678", thread_ts="none")["ok"]


@pytest.mark.parametrize("text", ["", "   ", None, 123])
def test_text_required_before_sending(tools, slack, text):
    assert run(tools, text=text, channel_id="C012345678", thread_ts="none")["error"] == "invalid_arguments"
    assert slack.calls == []


@pytest.mark.parametrize("tool, method, scope", [
    (POST, "chat.postMessage", "chat:write"),
    (LIST, "users.list", "users:read"),
])
def test_missing_scope_is_actionable(tools, slack, session, tool, method, scope):
    session(**SESSION)
    slack.script = {method: FakeApiError({"ok": False, "error": "missing_scope", "needed": scope})}
    result = run(tools, tool, text="hello")
    assert result["ok"] is False and result["error"] == "missing_scope"
    assert result["needed"] == scope and scope in result["action"]


def test_permalink_failure_does_not_turn_success_into_retry(tools, posted, session):
    session(**SESSION)
    posted.script["chat.getPermalink"] = FakeApiError({"ok": False, "error": "not_in_channel"})
    result = run(tools, text="hello")
    assert result["ok"] and result["ts"] == "3000.000100" and result["permalink"] is None
    assert posted.methods().count("chat.postMessage") == 1


def test_list_agents_filters_self_deleted_and_humans_across_pages(tools, slack):
    slack.script["auth.test"] = {"ok": True, "user_id": "U0SELF0000"}
    pages = iter([
        {"ok": True, "members": [
            {"id": "U0SELF0000", "is_bot": True},
            {"id": "U0DELETED0", "is_bot": True, "deleted": True},
            {"id": "U0HUMAN00", "is_bot": False},
        ], "response_metadata": {"next_cursor": "page2"}},
        {"ok": True, "members": [
            {"id": "U0AGENT00", "is_bot": True, "deleted": False, "name": "agent",
             "real_name": "Agent", "profile": {"api_app_id": "A012345678"}},
        ]},
    ])
    original = slack.api_call

    def paged(method, *, params=None):
        if method == "users.list":
            slack.calls.append((method, params))
            return next(pages)
        return original(method, params=params)

    slack.api_call = paged
    assert run(tools, LIST) == [{
        "user_id": "U0AGENT00", "name": "agent", "real_name": "Agent", "app_id": "A012345678",
    }]
    assert [params for method, params in slack.calls if method == "users.list"] == [
        {"limit": 200}, {"limit": 200, "cursor": "page2"},
    ]


def test_auth_failure_does_not_return_unfiltered_agents(tools, slack):
    slack.script["auth.test"] = FakeApiError({"ok": False, "error": "invalid_auth"})
    assert run(tools, LIST)["error"] == "invalid_auth"
    assert "users.list" not in slack.methods()


def test_new_tools_use_slack_admin_and_hide_without_token(tools, monkeypatch):
    for entry in tools.values():
        assert entry.toolset == "slack_admin"
        assert entry.check_fn()
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    assert not any(entry.check_fn() for entry in tools.values())


def test_real_client_posts_thread_and_mentions(tools, stub, session):
    session(**SESSION)
    stub.script = {
        "chat.postMessage": (200, {}, {"ok": True, "ts": "3000.000100"}),
        "chat.getPermalink": (200, {}, {"ok": True, "permalink": "https://slack/p3000"}),
    }
    result = run(tools, text="hello", mention_user_ids=["U012345678"])
    assert result["ok"] and result["permalink"] == "https://slack/p3000"
    assert stub.seen[0][:3] == ("POST", "chat.postMessage", {
        "channel": "C0SESSION", "thread_ts": "2000.000100", "text": "<@U012345678> hello",
    })
