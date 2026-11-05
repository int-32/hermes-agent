"""slack-channels plugin: loaded through real discovery, exercised through the registry handlers.

The Slack seam is ``slack_sdk.WebClient``, replaced by a scripted fake (or, for the wire-format tests, the real
client pointed at a local HTTP server).
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

TOKEN = "xoxb-test-secret-token"
CREATE = "agents.conversations.create"
ORIGIN = {"origin_channel_id": "C0ORIGIN", "origin_message_ts": "1712345678.000100"}


class FakeResponse(dict):
    def __init__(self, body, *, status_code=200, headers=None):
        super().__init__(body)
        self.status_code, self.headers = status_code, headers or {}


class FakeApiError(Exception):
    def __init__(self, body, **kwargs):
        super().__init__(f"The server responded with: {body}")
        self.response = FakeResponse(body, **kwargs)


class FakeSlack:
    """Records every call; a scripted Exception raises, anything else is returned."""

    def __init__(self):
        self.calls, self.script, self.tokens = [], {}, []

    def client(self, token):
        self.tokens.append(token)
        return SimpleNamespace(api_call=self.api_call, proxy=None)

    def api_call(self, method, *, params=None):
        self.calls.append((method, params))
        outcome = self.script.get(method, {"ok": True})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def methods(self):
        return [method for method, _ in self.calls]

    def params(self, method):
        return next(params for name, params in self.calls if name == method)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """The plugin's registry entries, loaded by a real PluginManager from a temp HERMES_HOME."""
    import hermes_yaml as yaml
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["slack-channels"]}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    manager = PluginManager()
    manager.discover_and_load()
    assert manager._plugins["slack-channels"].enabled, manager._plugins["slack-channels"].error
    return {name: registry.get_entry(name) for name in ("slack_create_code_channel", "slack_rename_channel")}


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setitem(sys.modules, "slack_sdk", SimpleNamespace(WebClient=lambda token: fake.client(token)))
    return fake


def run(tools, tool, **args):
    return json.loads(tools[tool].handler(args))


def test_tools_register_in_slack_admin_and_follow_the_token(tools, monkeypatch):
    for entry in tools.values():
        assert entry.toolset == "slack_admin"
        assert entry.check_fn() is True
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    assert not any(entry.check_fn() for entry in tools.values())


def test_create_makes_channel_then_posts_first_message(tools, slack):
    slack.script = {
        CREATE: {"ok": True, "channel_id": "C0NEW"},
        "chat.postMessage": {"ok": True, "ts": "111.222"},
        "conversations.info": {"ok": True, "channel": {"id": "C0NEW", "name": "fix-login"}},
        "chat.getPermalink": {"ok": True, "permalink": "https://slack/p111"},
    }
    result = run(tools, "slack_create_code_channel", **ORIGIN, name="Fix login", session_id="t_1", initial_message="hi")

    assert slack.methods()[:2] == [CREATE, "chat.postMessage"]
    assert slack.params(CREATE) == {**ORIGIN, "name": "Fix login", "session_id": "t_1"}
    assert slack.params("chat.postMessage") == {"channel": "C0NEW", "text": "hi"}
    assert slack.tokens == [TOKEN]
    assert result == {
        "ok": True, "channel_id": "C0NEW", "name": "fix-login", "message_ts": "111.222",
        "requested_name": "Fix login", "permalink": "https://slack/p111", "origin_source": "arguments",
    }


def test_create_leaves_naming_and_privacy_to_slack_unless_asked(tools, slack):
    slack.script = {CREATE: {"ok": True, "channel_id": "C0NEW"}}
    run(tools, "slack_create_code_channel", **ORIGIN)
    assert slack.params(CREATE) == ORIGIN
    assert slack.params("chat.postMessage")["text"]

    slack.calls.clear()
    run(tools, "slack_create_code_channel", **ORIGIN, is_private=False)
    assert slack.params(CREATE)["is_private"] is False


def test_create_reports_missing_name_lookup_without_failing(tools, slack):
    needed = FakeApiError({"ok": False, "error": "missing_scope", "needed": "channels:read", "provided": "chat:write"})
    slack.script = {
        CREATE: {"ok": True, "channel_id": "C0NEW"}, "chat.postMessage": {"ok": True, "ts": "1.2"},
        "conversations.info": needed,
    }
    result = run(tools, "slack_create_code_channel", **ORIGIN)
    assert result["ok"] is True and result["name"] is None
    assert result["name_unavailable"]["needed"] == "channels:read"


def test_feature_disabled_says_how_to_enable_and_posts_nothing(tools, slack):
    slack.script = {CREATE: FakeApiError({"ok": False, "error": "feature_disabled"})}
    result = run(tools, "slack_create_code_channel", **ORIGIN)
    assert result["ok"] is False and result["error"] == "feature_disabled"
    assert "features.code_channels.enabled=true" in result["message"]
    assert "code_channels:manage" in result["message"]
    assert slack.methods() == [CREATE]


def test_create_missing_scope_returns_needed_and_provided(tools, slack):
    slack.script = {CREATE: FakeApiError({
        "ok": False, "error": "missing_scope", "needed": "code_channels:manage", "provided": "chat:write"})}
    result = run(tools, "slack_create_code_channel", **ORIGIN)
    assert (result["error"], result["needed"], result["provided"]) == ("missing_scope", "code_channels:manage", "chat:write")
    assert "code_channels:manage" in result["action"]


def test_failed_first_message_is_partial_and_keeps_channel_id(tools, slack):
    slack.script = {
        CREATE: {"ok": True, "channel_id": "C0NEW"},
        "chat.postMessage": FakeApiError({"ok": False, "error": "not_in_channel"}),
    }
    result = run(tools, "slack_create_code_channel", **ORIGIN)
    assert result["ok"] is False and result["partial"] is True and result["channel_created"] is True
    assert result["channel_id"] == "C0NEW"
    assert result["error"] == "not_in_channel"


def test_rename_confirms_by_reading_back(tools, slack):
    slack.script = {"conversations.info": {"ok": True, "channel": {"name": "new-name"}}}
    result = run(tools, "slack_rename_channel", channel_id="C1", new_name="new-name")
    assert slack.calls == [
        ("conversations.rename", {"channel": "C1", "name": "new-name"}),
        ("conversations.info", {"channel": "C1"}),
    ]
    assert result == {"ok": True, "channel_id": "C1", "name": "new-name", "verified": True}


def test_rename_readback_mismatch_is_not_success(tools, slack):
    slack.script = {"conversations.info": {"ok": True, "channel": {"name": "old-name"}}}
    result = run(tools, "slack_rename_channel", channel_id="C1", new_name="new-name")
    assert result["ok"] is False and result["error"] == "rename_readback_mismatch"
    assert result["name"] == "old-name" and result["rename_reported_success"] is True


def test_rename_unreadable_afterwards_says_rename_went_through(tools, slack):
    slack.script = {"conversations.info": FakeApiError({
        "ok": False, "error": "missing_scope", "needed": "channels:read", "provided": "channels:manage"})}
    result = run(tools, "slack_rename_channel", channel_id="C1", new_name="new-name")
    assert result["ok"] is False and result["error"] == "rename_readback_failed"
    assert result["slack_error"] == "missing_scope" and result["needed"] == "channels:read"
    assert result["rename_reported_success"] is True
    assert "channels:read" in result["action"]


def test_rename_missing_scope_explains_both_channel_types_and_skips_readback(tools, slack):
    slack.script = {"conversations.rename": FakeApiError({
        "ok": False, "error": "missing_scope", "needed": "channels:manage", "provided": "chat:write"})}
    result = run(tools, "slack_rename_channel", channel_id="C1", new_name="x")
    assert result["needed"] == "channels:manage" and result["provided"] == "chat:write"
    assert "channels:manage" in result["action"] and "groups:write" in result["action"]
    assert slack.methods() == ["conversations.rename"]


def test_rate_limit_carries_retry_after(tools, slack):
    slack.script = {"conversations.rename": FakeApiError(
        {"ok": False, "error": "ratelimited"}, status_code=429, headers={"Retry-After": "30"})}
    result = run(tools, "slack_rename_channel", channel_id="C1", new_name="x")
    assert result["error"] == "ratelimited" and result["retry_after_seconds"] == 30


def test_unmapped_slack_error_is_passed_through(tools, slack):
    slack.script = {"conversations.rename": FakeApiError({"ok": False, "error": "brand_new_error"})}
    result = run(tools, "slack_rename_channel", channel_id="C1", new_name="x")
    assert result["ok"] is False and result["error"] == "brand_new_error"
    assert "brand_new_error" in result["message"] and result["action"]


def test_token_never_appears_in_results(tools, slack):
    slack.script = {"conversations.rename": RuntimeError(f"connection failed for Bearer {TOKEN}")}
    raw = tools["slack_rename_channel"].handler({"channel_id": "C1", "new_name": "x"})
    assert TOKEN not in raw
    assert json.loads(raw)["error"] == "slack_request_failed"
    assert "connection failed" in json.loads(raw)["message"]


def test_token_comes_from_the_active_profile_scope(tools, slack):
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    scope = set_secret_scope({"SLACK_BOT_TOKEN": "xoxb-profile-b"})
    try:
        run(tools, "slack_rename_channel", channel_id="C1", new_name="x")
    finally:
        reset_secret_scope(scope)
    run(tools, "slack_rename_channel", channel_id="C1", new_name="x")
    assert slack.tokens == ["xoxb-profile-b", TOKEN]


def test_multiplexing_without_a_scope_fails_closed(tools, slack):
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context

    context = set_multiplex_context(True)
    try:
        result = run(tools, "slack_rename_channel", channel_id="C1", new_name="x")
    finally:
        reset_multiplex_context(context)
    assert result["error"] == "secret_scope_unavailable"
    assert slack.calls == []


@pytest.mark.parametrize("value, code", [("", "missing_token"), ("xoxb-a,xoxb-b", "multiple_workspace_tokens")])
def test_unusable_token_value_is_reported(tools, slack, monkeypatch, value, code):
    monkeypatch.setenv("SLACK_BOT_TOKEN", value)
    result = run(tools, "slack_rename_channel", channel_id="C1", new_name="x")
    assert result["ok"] is False and result["error"] == code
    assert slack.calls == []


def test_missing_sdk_is_reported(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "slack_sdk", None)
    result = run(tools, "slack_create_code_channel", **ORIGIN)
    assert result["error"] == "slack_sdk_unavailable"
    assert "hermes pm install --extra slack" in result["action"]


SESSION = {"platform": "slack", "chat_id": "C0SESSION", "message_id": "2000.000200", "thread_id": "2000.000100"}


@pytest.fixture
def session(monkeypatch):
    """Bind session vars like a gateway turn; starts from no context, and env values must not leak in."""
    from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars

    for name in ("PLATFORM", "CHAT_ID", "MESSAGE_ID", "THREAD_ID"):
        monkeypatch.delenv(f"HERMES_SESSION_{name}", raising=False)
    reset_session_vars()
    bound = []

    def bind(**values):
        bound.extend(set_session_vars(**values))

    yield bind
    clear_session_vars(bound)
    reset_session_vars()


def test_omitted_origin_comes_from_the_slack_turn(tools, slack, session):
    session(**SESSION)
    slack.script = {CREATE: {"ok": True, "channel_id": "C0NEW"}}
    result = run(tools, "slack_create_code_channel", name="fix-login", session_id="t_1")

    assert slack.params(CREATE) == {
        "origin_channel_id": "C0SESSION", "origin_message_ts": "2000.000200", "name": "fix-login", "session_id": "t_1",
    }
    assert result["ok"] is True and result["origin_source"] == "session"
    assert (result["origin_channel_id"], result["origin_message_ts"]) == ("C0SESSION", "2000.000200")


def test_turn_without_message_id_falls_back_to_thread(tools, slack, session):
    session(**{**SESSION, "message_id": ""})
    slack.script = {CREATE: {"ok": True, "channel_id": "C0NEW"}}
    result = run(tools, "slack_create_code_channel")
    assert slack.params(CREATE) == {"origin_channel_id": "C0SESSION", "origin_message_ts": "2000.000100"}
    assert result["origin_message_ts"] == "2000.000100"


def test_explicit_origin_beats_the_session(tools, slack, session):
    session(**SESSION)
    slack.script = {CREATE: {"ok": True, "channel_id": "C0NEW"}}
    result = run(tools, "slack_create_code_channel", **ORIGIN)
    assert slack.params(CREATE) == ORIGIN
    assert result["origin_source"] == "arguments"


@pytest.mark.parametrize("partial", [
    {"origin_channel_id": "C0ORIGIN"}, {"origin_message_ts": "1712345678.000100"},
])
def test_one_origin_argument_is_rejected_even_inside_a_slack_turn(tools, slack, session, partial):
    session(**SESSION)
    result = run(tools, "slack_create_code_channel", **partial)
    assert result["ok"] is False and result["error"] == "invalid_arguments"
    assert "together" in result["message"] and "Slack conversation" in result["action"]
    assert slack.calls == []


@pytest.mark.parametrize("values", [
    None,
    {**SESSION, "platform": "telegram"},
    {"platform": "slack", "chat_id": "", "message_id": "2000.000200"},
    {"platform": "slack", "chat_id": "C0SESSION", "message_id": "", "thread_id": ""},
], ids=["no-context", "other-platform", "no-channel", "no-message"])
def test_origin_unknown_without_a_usable_slack_turn(tools, slack, session, values):
    if values:
        session(**values)
    result = run(tools, "slack_create_code_channel", name="fix-login")
    assert result["ok"] is False and result["error"] == "origin_unknown"
    assert result["message"] and "origin_channel_id" in result["action"]
    assert slack.calls == []


def test_slack_failure_shows_the_inferred_origin(tools, slack, session):
    session(**SESSION)
    slack.script = {CREATE: FakeApiError({"ok": False, "error": "invalid_origin_link"})}
    result = run(tools, "slack_create_code_channel")
    assert result["error"] == "invalid_origin_link" and result["origin_source"] == "session"
    assert result["origin_channel_id"] == "C0SESSION"


def test_schema_does_not_require_origin(tools):
    assert not tools["slack_create_code_channel"].schema["parameters"].get("required")


class _SlackStub(BaseHTTPRequestHandler):
    def _answer(self, query):
        method = urlparse(self.path).path.rsplit("/", 1)[-1]
        self.server.seen.append((self.command, method, {k: v[0] for k, v in query.items()},
                                 self.headers.get("Authorization")))
        status, headers, body = self.server.script.get(method, (200, {}, {"ok": True}))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self._answer({**parse_qs(urlparse(self.path).query), **parse_qs(body)})

    def do_GET(self):
        self._answer(parse_qs(urlparse(self.path).query))

    def log_message(self, *args):
        pass


@pytest.fixture
def stub(monkeypatch):
    """The real WebClient talking to a local server, so real SlackApiError and request encoding are exercised."""
    sdk = pytest.importorskip("slack_sdk")
    server = HTTPServer(("127.0.0.1", 0), _SlackStub)
    server.seen, server.script = [], {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/api/"
    real = sdk.WebClient
    monkeypatch.setattr(sdk, "WebClient", lambda token: real(token=token, base_url=base_url))
    yield server
    server.shutdown()
    server.server_close()


def test_real_client_sends_create_and_first_message(tools, stub):
    stub.script = {
        CREATE: (200, {}, {"ok": True, "channel_id": "C0NEW"}),
        "chat.postMessage": (200, {}, {"ok": True, "ts": "1.2"}),
    }
    result = run(tools, "slack_create_code_channel", **ORIGIN, is_private=True, initial_message="hello")
    assert result["ok"] is True and result["channel_id"] == "C0NEW" and result["message_ts"] == "1.2"

    verb, method, params, auth = stub.seen[0]
    assert (verb, method, auth) == ("POST", CREATE, f"Bearer {TOKEN}")
    assert params == {**ORIGIN, "is_private": "1"}
    assert stub.seen[1][:3] == ("POST", "chat.postMessage", {"channel": "C0NEW", "text": "hello"})
    assert stub.seen[2][1:3] == ("conversations.info", {"channel": "C0NEW"})


def test_real_client_errors_become_structured_results(tools, stub):
    stub.script = {
        CREATE: (200, {}, {"ok": False, "error": "feature_disabled"}),
        "conversations.rename": (429, {"Retry-After": "7"}, {"ok": False, "error": "ratelimited"}),
    }
    created = run(tools, "slack_create_code_channel", **ORIGIN)
    assert created["error"] == "feature_disabled" and "features.code_channels.enabled" in created["message"]

    renamed = run(tools, "slack_rename_channel", channel_id="C1", new_name="x")
    assert renamed["error"] == "ratelimited" and renamed["retry_after_seconds"] == 7
