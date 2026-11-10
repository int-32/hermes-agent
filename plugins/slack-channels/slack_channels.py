"""Handlers for slack_admin. Successes are JSON objects (or an agent array); failures add ``ok:false``, ``error`` (the
Slack code when Slack gave one), ``message`` and ``action``. The bot token is read per call from the active
profile scope and never reaches a return value."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from agent.retry_utils import parse_retry_after_seconds
from agent.secret_scope import UnscopedSecretError, get_secret
from gateway.session_context import get_session_env

_TOKEN_ENV = "SLACK_BOT_TOKEN"
_REINSTALL = "then click Reinstall to Workspace in the Slack app settings"
_DEFAULT_INITIAL_MESSAGE = "This Code Channel was created from the linked Slack message. Work on the task continues here."

_CODE_CHANNEL_NOT_ENABLED = (
    "Code Channel is not enabled: set features.code_channels.enabled=true in the Slack app manifest and "
    f"request the code_channels:manage bot scope, {_REINSTALL}."
)
_CREATE_SCOPES = f"Add the code_channels:manage bot scope, {_REINSTALL}."
_POST_SCOPES = f"Add the chat:write bot scope, {_REINSTALL}."
_USERS_SCOPES = f"Add the users:read bot scope, {_REINSTALL}."
_RENAME_SCOPES = (
    f"Add the bot scope for the channel type (public: channels:manage, private: groups:write), {_REINSTALL}."
)
_READ_SCOPES = (
    f"Add the bot scope for the channel type (public: channels:read, private: groups:read), {_REINSTALL}."
)

_INVALID_NAME_ACTION = "Use lowercase letters, numbers, hyphens and underscores, at most 80 characters."
_MAY_HAVE_APPLIED = "Part of the operation may have succeeded: check the channel before retrying."

# Slack code -> (what happened, what to do).
_SLACK_ERRORS: dict[str, tuple[str, str]] = {
    "feature_disabled": (_CODE_CHANNEL_NOT_ENABLED, "Enable the feature as described, then call this tool again."),
    "not_authorized": (
        "The bot may not do this: Slack only lets the channel's creator, a Workspace Admin or a Channel Manager rename it.",
        "Rename a channel the bot created, or have an admin grant the bot's user that role.",
    ),
    "restricted_action": (
        "A workspace policy blocks this action.",
        "Ask a Workspace Admin to allow it in the workspace permission settings.",
    ),
    "no_permission": (
        "The bot lacks permission for this; it may need to be a member of the conversation.",
        "Invite the bot to the channel or check the app's scopes.",
    ),
    "access_denied": ("Slack denied access to a resource in this request.", "Check that the bot can access the channel."),
    "not_in_channel": ("The bot is not a member of the channel.", "Invite the bot to the channel, then try again."),
    "channel_not_found": (
        "Slack cannot find this channel with this bot token.",
        "Check the channel ID, and that the bot is in the channel if it is private.",
    ),
    "name_taken": ("That channel name is already in use.", "Pick a different name."),
    "is_archived": ("Archived channels cannot be changed.", "Unarchive the channel first or use another one."),
    "method_not_supported_for_channel_type": (
        "This conversation type does not support this method.",
        "Use a public or private channel.",
    ),
    "invalid_name": ("Slack rejected the channel name.", _INVALID_NAME_ACTION),
    "invalid_name_maxlength": ("The channel name is too long.", _INVALID_NAME_ACTION),
    "invalid_name_punctuation": ("The channel name contains only punctuation.", _INVALID_NAME_ACTION),
    "invalid_name_required": ("The channel name is empty.", _INVALID_NAME_ACTION),
    "invalid_name_specials": ("The channel name has disallowed characters or uppercase letters.", _INVALID_NAME_ACTION),
    "invalid_origin_link": (
        "Slack could not link the origin: the channel is inaccessible or the message does not exist. No channel was created.",
        "Check origin_channel_id and origin_message_ts, and that the bot can read that channel.",
    ),
    "origin_channel_externally_shared": (
        "The origin channel is a Slack Connect channel, which cannot be an origin.",
        "Start from a message in a channel that is not externally shared.",
    ),
    "origin_channel_is_file_channel": (
        "The origin is a file or canvas comment, which cannot be an origin.",
        "Start from a regular channel message.",
    ),
    "channel_not_viewable": (
        "A channel already exists for this session_id, but the bot cannot see it.",
        "Use another session_id or add the bot to that channel.",
    ),
    "channel_creation_failed": ("Slack failed to create the channel.", "Retry later with the same session_id."),
    "invite_failed": (
        "The channel was created, but the agent could not be invited.",
        "Retry with the same session_id to get the same channel, or check it in Slack.",
    ),
    "open_failed": (
        "The channel was created, but was not added to the sidebar.",
        "Retry with the same session_id to get the same channel, or check it in Slack.",
    ),
    "user_not_enabled": ("This user is not enabled for the feature.", "Ask a Workspace Admin to enable it."),
    "ratelimited": ("Slack rate limited this request.", "Wait for retry_after_seconds, then try again."),
    "invalid_auth": ("Slack rejected the bot token.", f"Check {_TOKEN_ENV} in this profile's .env."),
    "not_authed": ("Slack received no token.", f"Check {_TOKEN_ENV} in this profile's .env."),
    "account_inactive": ("The bot token belongs to a deleted user or workspace.", f"Replace {_TOKEN_ENV}."),
    "token_revoked": ("The bot token was revoked or the app was removed.", f"Reinstall the app and update {_TOKEN_ENV}."),
    "token_expired": ("The bot token has expired.", f"Refresh {_TOKEN_ENV}."),
    "not_allowed_token_type": ("This method does not accept this kind of token.", f"Use a bot token (xoxb-) in {_TOKEN_ENV}."),
    "internal_error": ("Slack reported an internal error.", _MAY_HAVE_APPLIED),
    "fatal_error": ("Slack reported a server error.", _MAY_HAVE_APPLIED),
    "service_unavailable": ("Slack is temporarily unavailable.", "Retry later."),
}

# Slack documents these as possibly leaving a channel behind.
_CREATE_MAY_EXIST = frozenset({"invite_failed", "open_failed", "internal_error", "fatal_error"})


class _SlackFailure(Exception):
    """One failed Slack call, reduced to fields safe to return."""

    def __init__(self, code: str, *, method: str = "", message: str = "", action: str = "", **fields: Any):
        super().__init__(code)
        self.code, self.method, self.message, self.action, self.fields = code, method, message, action, fields

    def payload(self, scopes_action: str = "") -> dict:
        message, action = self.message, self.action
        if self.code == "missing_scope":
            needed = self.fields.get("needed") or "unknown"
            message = f"The bot token lacks the scope {self.method} needs (needed: {needed})."
            action = scopes_action
        elif not message:
            message, action = _SLACK_ERRORS.get(self.code, (
                f"Slack {self.method} returned {self.code}.",
                f"See https://docs.slack.dev/reference/methods/{self.method}/ for this error.",
            ))
        return {"ok": False, "error": self.code, "message": message, "action": action,
                **({"slack_method": self.method} if self.method else {}), **self.fields}


def _failure_from_api_error(method: str, response: Any) -> _SlackFailure:
    status = getattr(response, "status_code", None)
    code = response.get("error") or ("ratelimited" if status == 429 else f"http_{status}")
    fields = {key: response[key] for key in ("needed", "provided", "arg") if response.get(key)}
    retry_after = parse_retry_after_seconds(getattr(response, "headers", None))
    if retry_after is not None:
        fields["retry_after_seconds"] = retry_after
    return _SlackFailure(code, method=method, **fields)


def _resolve_token() -> str:
    try:
        raw = get_secret(_TOKEN_ENV) or ""
    except UnscopedSecretError:
        raise _SlackFailure(
            "secret_scope_unavailable",
            message=f"{_TOKEN_ENV} cannot be read here: no profile secret scope is active in this multiplexing process.",
            action="Call this tool from a turn bound to a profile.",
        ) from None
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    if not tokens:
        raise _SlackFailure(
            "missing_token", message=f"{_TOKEN_ENV} is not set for this profile.",
            action=f"Add {_TOKEN_ENV} (xoxb-) to this profile's .env.",
        )
    if len(tokens) > 1:
        raise _SlackFailure(
            "multiple_workspace_tokens",
            message=f"{_TOKEN_ENV} lists several workspaces and this tool cannot tell which one owns the channel.",
            action=f"Use a profile whose {_TOKEN_ENV} holds a single bot token.",
        )
    return tokens[0]


def _new_client(token: str) -> Any:
    try:
        from slack_sdk import WebClient
    except ImportError:
        raise _SlackFailure(
            "slack_sdk_unavailable", message="The slack-sdk package is not installed in this Hermes environment.",
            action="Run `hermes pm install --extra slack`.",
        ) from None
    from gateway.platforms.base import resolve_proxy_url

    client = WebClient(token=token)
    # WebClient reloads HTTPS_PROXY from the env at construction, bypassing NO_PROXY and gateway.trust_env, so the
    # route only matches the Slack adapter's when set afterwards. Its urllib transport has no SOCKS support.
    proxy = resolve_proxy_url(target_hosts="slack.com")
    client.proxy = proxy if proxy and proxy.lower().startswith(("http://", "https://")) else None
    return client


def _open_caller() -> Callable[..., Any]:
    """A ``call(method, **params)`` bound to this call's token, raising ``_SlackFailure``."""
    token = _resolve_token()
    client = _new_client(token)

    def call(method: str, **params: Any) -> Any:
        try:
            return client.api_call(method, params=params)
        except Exception as exc:  # health: allow BLE001 -- tool boundary: every failure becomes a structured result, and a logged traceback could carry the token
            response = getattr(exc, "response", None)
            if response is not None and callable(getattr(response, "get", None)):
                raise _failure_from_api_error(method, response) from None
            detail = str(exc).replace(token, "[REDACTED]")
            raise _SlackFailure(
                "slack_request_failed", method=method,
                message=f"The request to Slack {method} failed before Slack answered ({type(exc).__name__}: {detail}).",
                action="Check network access to slack.com, then retry.",
            ) from None

    return call


def _result(payload: dict | list) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _missing_args(*names: str) -> str:
    return _result({
        "ok": False, "error": "invalid_arguments",
        "message": f"Required argument missing or empty: {', '.join(names)}.",
        "action": "Pass non-empty strings for these arguments.",
    })


def token_configured() -> bool:
    return bool(get_secret(_TOKEN_ENV))


def _message_target(args: dict) -> tuple[str, Optional[str]]:
    channel = _text(args, "channel_id")
    thread = _text(args, "thread_ts")
    if channel.startswith(("U", "D")):
        raise _SlackFailure(
            "invalid_target", message="Bots cannot DM other bots (U/D targets).",
            action="Use a public or private C/G channel ID shared by the bots.",
        )
    if "channel_id" not in args or "thread_ts" not in args:
        if get_session_env("HERMES_SESSION_PLATFORM") != "slack":
            raise _SlackFailure(
                "invalid_arguments", message="Channel and thread defaults require a current Slack conversation.",
                action='Pass channel_id and thread_ts explicitly ("none" for a top-level message), or call from Slack.',
            )
        if "channel_id" not in args:
            channel = get_session_env("HERMES_SESSION_CHAT_ID") or ""
        if "thread_ts" not in args:
            thread = get_session_env("HERMES_SESSION_THREAD_ID") or get_session_env("HERMES_SESSION_MESSAGE_ID") or ""
    if not re.fullmatch(r"[CG][A-Z0-9]+", channel):
        raise _SlackFailure(
            "invalid_target", message="Only C/G channel IDs are supported. Bots cannot DM other bots (U/D targets).",
            action="Use a public or private channel ID shared by the bots.",
        )
    if not thread:
        raise _SlackFailure(
            "invalid_arguments", message="No thread timestamp is available.",
            action='Pass thread_ts, or pass the string "none" explicitly to post a top-level message.',
        )
    return channel, None if thread == "none" else thread


def post_message(args: dict, **_kwargs: Any) -> str:
    text = _text(args, "text")
    if not text:
        return _missing_args("text")
    mentions = args.get("mention_user_ids", [])
    if not isinstance(mentions, list) or any(
        not isinstance(user, str) or not re.fullmatch(r"U[A-Z0-9]{8,}", user) for user in mentions
    ):
        return _result({
            "ok": False, "error": "invalid_arguments", "message": "mention_user_ids must be an array of Slack user IDs.",
            "action": "Use IDs matching ^U[A-Z0-9]{8,}$, not names or mention markup.",
        })
    if mentions:
        text = " ".join(f"<@{user}>" for user in mentions) + " " + text
    try:
        channel, thread = _message_target(args)
        call = _open_caller()
        params = {"channel": channel, "text": text}
        if thread is not None:
            params["thread_ts"] = thread
        response = call("chat.postMessage", **params)
    except _SlackFailure as failure:
        return _result(failure.payload(_POST_SCOPES))
    ts = response.get("ts")
    if not ts:
        return _result({
            "ok": False, "error": "unexpected_response", "message": "Slack accepted the post but returned no ts.",
            "action": "Check the channel before retrying; the message may already have been posted.",
        })
    # A link lookup must not make an already-posted message look safe to retry.
    try:
        permalink = call("chat.getPermalink", channel=channel, message_ts=ts).get("permalink")
    except _SlackFailure:
        permalink = None
    return _result({"ok": True, "channel_id": channel, "ts": ts, "thread_ts": thread, "permalink": permalink})


def list_agents(args: dict, **_kwargs: Any) -> str:
    try:
        call = _open_caller()
        self_id = call("auth.test").get("user_id")
        agents = []
        params: dict = {"limit": 200}
        while True:
            response = call("users.list", **params)
            for member in response.get("members", []):
                if not member.get("is_bot") or member.get("deleted") or member.get("id") == self_id:
                    continue
                profile = member.get("profile") or {}
                agents.append({
                    "user_id": member.get("id"), "name": member.get("name", ""),
                    "real_name": member.get("real_name") or profile.get("real_name", ""),
                    "app_id": profile.get("api_app_id") or member.get("app_id", ""),
                })
            cursor = (response.get("response_metadata") or {}).get("next_cursor", "").strip()
            if not cursor:
                break
            params = {"limit": 200, "cursor": cursor}
    except _SlackFailure as failure:
        return _result(failure.payload(_USERS_SCOPES))
    return _result(agents)


def _read_name(call: Callable[..., Any], channel_id: str) -> str:
    channel = call("conversations.info", channel=channel_id).get("channel") or {}
    return channel.get("name") or ""


def _created_payload(call: Callable[..., Any], channel_id: str, requested_name: str, message_ts: Optional[str]) -> dict:
    """Success result. Create only returns channel_id, so the name and permalink are best-effort lookups; a failed
    one is reported on the result rather than failing a channel that was already created."""
    payload: dict = {"ok": True, "channel_id": channel_id, "name": None, "message_ts": message_ts}
    if requested_name:
        payload["requested_name"] = requested_name
    try:
        payload["name"] = _read_name(call, channel_id) or None
    except _SlackFailure as failure:
        payload["name_unavailable"] = {k: v for k, v in failure.payload(_READ_SCOPES).items() if k != "ok"}
    try:
        permalink = call("chat.getPermalink", channel=channel_id, message_ts=message_ts).get("permalink")
    except _SlackFailure:
        permalink = None
    if permalink:
        payload["permalink"] = permalink
    return payload


def _resolve_origin(args: dict) -> tuple[str, str, dict]:
    """(channel, ts, fields for the result): both arguments, or both from the current Slack turn, never a mix."""
    channel, ts = _text(args, "origin_channel_id"), _text(args, "origin_message_ts")
    if channel and ts:
        return channel, ts, {"origin_source": "arguments"}
    if channel or ts:
        raise _SlackFailure(
            "invalid_arguments", message="origin_channel_id and origin_message_ts must be passed together.",
            action="Pass both, or omit both to use the current channel and message when called from a Slack conversation.",
        )
    channel = get_session_env("HERMES_SESSION_CHAT_ID")
    # Slash commands have no triggering message, and outside a thread no thread ts either.
    ts = get_session_env("HERMES_SESSION_MESSAGE_ID") or get_session_env("HERMES_SESSION_THREAD_ID")
    if get_session_env("HERMES_SESSION_PLATFORM") != "slack" or not channel or not ts:
        raise _SlackFailure(
            "origin_unknown", message="No origin message: this call has no current Slack channel and message to link to.",
            action="Call this tool from a Slack conversation, or pass both origin_channel_id and origin_message_ts.",
        )
    return channel, ts, {"origin_source": "session", "origin_channel_id": channel, "origin_message_ts": ts}


def create_code_channel(args: dict, **_kwargs: Any) -> str:
    is_private = args.get("is_private")
    if is_private is not None and not isinstance(is_private, bool):
        return _result({
            "ok": False, "error": "invalid_arguments", "message": "is_private must be a boolean.",
            "action": "Pass true or false, or omit it to inherit the origin channel's privacy.",
        })
    try:
        origin_channel_id, origin_message_ts, origin = _resolve_origin(args)
    except _SlackFailure as failure:
        return _result(failure.payload())

    name, session_id = _text(args, "name"), _text(args, "session_id")
    params: dict = {"origin_channel_id": origin_channel_id, "origin_message_ts": origin_message_ts}
    if name:
        params["name"] = name
    if session_id:
        params["session_id"] = session_id
    if is_private is not None:
        params["is_private"] = is_private
    text = _text(args, "initial_message") or _DEFAULT_INITIAL_MESSAGE
    return _result({**_create_and_post(params, text), **origin})


def _create_and_post(params: dict, text: str) -> dict:
    name, session_id = params.get("name", ""), params.get("session_id", "")
    try:
        call = _open_caller()
        channel_id = call("agents.conversations.create", **params).get("channel_id")
    except _SlackFailure as failure:
        payload = failure.payload(_CREATE_SCOPES)
        if failure.code in _CREATE_MAY_EXIST:
            payload["channel_may_exist"] = True
        return payload
    if not channel_id:
        return {
            "ok": False, "error": "unexpected_response", "slack_method": "agents.conversations.create",
            "message": "Slack reported success but returned no channel_id.",
            "action": "Check Slack for a new channel before retrying.", "channel_may_exist": True,
        }

    try:
        message_ts = call("chat.postMessage", channel=channel_id, text=text).get("ts")
    except _SlackFailure as failure:
        return _partial_create(failure, channel_id, name, session_id)
    return _created_payload(call, channel_id, name, message_ts)


def _partial_create(failure: _SlackFailure, channel_id: str, name: str, session_id: str) -> dict:
    payload = failure.payload(_POST_SCOPES)
    retry = (
        "Calling this tool again with the same session_id returns this channel and retries the message."
        if session_id else "Do not call this tool again: without a session_id that creates a second channel."
    )
    payload.update({
        "partial": True, "channel_created": True, "channel_id": channel_id, "stage": "initial_message",
        "message": f"Channel {channel_id} was created, but its first message failed: {payload['message']}",
        "action": f"{payload['action']} {retry}",
    })
    if name:
        payload["requested_name"] = name
    return payload


def rename_channel(args: dict, **_kwargs: Any) -> str:
    channel_id, new_name = _text(args, "channel_id"), _text(args, "new_name")
    missing = [k for k, v in (("channel_id", channel_id), ("new_name", new_name)) if not v]
    if missing:
        return _missing_args(*missing)

    try:
        call = _open_caller()
        call("conversations.rename", channel=channel_id, name=new_name)
    except _SlackFailure as failure:
        return _result(failure.payload(_RENAME_SCOPES))

    unconfirmed = {"channel_id": channel_id, "requested_name": new_name, "rename_reported_success": True}
    try:
        name = _read_name(call, channel_id)
    except _SlackFailure as failure:
        payload = failure.payload(_READ_SCOPES)
        return _result({
            **payload, **unconfirmed, "partial": True, "error": "rename_readback_failed", "slack_error": failure.code,
            "message": f"Slack accepted the rename, but reading the name back failed: {payload['message']}",
            "action": f"{payload['action']} Do not retry the rename; confirm the name in Slack.",
        })
    if name != new_name:
        return _result({
            **unconfirmed, "ok": False, "error": "rename_readback_mismatch", "name": name,
            "slack_method": "conversations.info",
            "message": f"The rename returned success but the read-back is inconsistent: requested {new_name!r}, read back {name!r}.",
            "action": "Check the channel in Slack; Slack may have normalized the name or another rename may have raced this one.",
        })
    return _result({"ok": True, "channel_id": channel_id, "name": name, "verified": True})
