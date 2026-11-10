"""Slack channel management and bot messaging (toolset ``slack_admin``)."""

from __future__ import annotations

from . import slack_channels as impl

_TOOLSET = "slack_admin"

_CREATE_SCHEMA = {
    "name": "slack_create_code_channel",
    "description": (
        "Create a Slack Code Channel natively linked to the Slack message that started a task, then post "
        "the first message in it. Use when work from a Slack conversation should move to its own channel "
        "(one task, one channel). In a Slack conversation, omit the origin arguments to link the current "
        "channel and message. The origin message's author is invited automatically. Returns channel_id, "
        "message_ts and origin_source. On ok:false read error/message/action; partial:true means the channel exists and "
        "channel_id is still valid, so do not create another. Calling again with the same session_id "
        "returns the same channel and posts initial_message again, so only repeat it after a failure."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin_channel_id": {
                "type": "string",
                "description": (
                    "Slack channel ID holding the origin message, e.g. C0123ABC. Pass together with "
                    "origin_message_ts, or omit both in a Slack conversation to infer them from the current "
                    "channel and message. A channel name is not accepted."
                ),
            },
            "origin_message_ts": {
                "type": "string",
                "description": (
                    "Slack ts of the origin message, e.g. 1712345678.000100. Pass together with "
                    "origin_channel_id, or omit both in a Slack conversation to infer them."
                ),
            },
            "name": {
                "type": "string",
                "description": "Display name. Omit to let Slack name the channel from the origin message.",
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Idempotency key: an existing channel for this value is returned instead of a new one. "
                    "Pass a stable value such as the task ID; never generate a random one."
                ),
            },
            "initial_message": {
                "type": "string",
                "description": "First message to post in the new channel. Omit for a short default.",
            },
            "is_private": {
                "type": "boolean",
                "description": "Only set when explicitly requested. Omit to inherit the origin channel's privacy.",
            },
        },
        "additionalProperties": False,
    },
}

_RENAME_SCHEMA = {
    "name": "slack_rename_channel",
    "description": (
        "Rename a Slack channel and confirm the result by reading the name back, e.g. to give a task channel "
        "a readable name. ok:true means the read-back matched. On ok:false read error/message/action; "
        "rename_reported_success:true means Slack accepted the rename but it could not be confirmed, so do "
        "not simply retry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "Slack channel ID to rename, e.g. C0123ABC."},
            "new_name": {
                "type": "string",
                "description": "New name: lowercase letters, numbers, hyphens and underscores, at most 80 characters.",
            },
        },
        "required": ["channel_id", "new_name"],
        "additionalProperties": False,
    },
}


_POST_SCHEMA = {
    "name": "slack_post_message",
    "description": (
        "Post a message in a Slack public or private channel, optionally mentioning agent bots. "
        "Defaults to the current Slack channel and thread (or triggering message). "
        "Bots cannot DM each other. Returns ok, channel_id, ts, thread_ts and permalink; "
        "on failure read error/message/action. Do not repeat a successful post."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Message body (required)."},
            "channel_id": {"type": "string", "description": "C/G channel ID; omit for the current Slack channel. No U/D targets."},
            "thread_ts": {
                "type": "string",
                "description": 'Thread timestamp; omit for the current Slack thread, then triggering message. Only "none" posts top-level.',
            },
            "mention_user_ids": {
                "type": "array", "items": {"type": "string", "pattern": "^U[A-Z0-9]{8,}$"},
                "description": "User IDs to prepend as <@USER_ID> mentions in the supplied order.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}

_LIST_AGENTS_SCHEMA = {
    "name": "slack_list_agents",
    "description": (
        "List active Slack bot users in this workspace, excluding this bot and deleted users. "
        "Use to resolve an agent bot's user ID for mentions. Returns an array of "
        "{user_id, name, real_name, app_id}; on failure returns ok:false with error/message/action."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def register(ctx) -> None:
    for schema, handler, emoji in (
        (_CREATE_SCHEMA, impl.create_code_channel, "🧵"),
        (_RENAME_SCHEMA, impl.rename_channel, "🏷️"),
        (_POST_SCHEMA, impl.post_message, "💬"),
        (_LIST_AGENTS_SCHEMA, impl.list_agents, "🤖"),
    ):
        ctx.register_tool(
            name=schema["name"], toolset=_TOOLSET, schema=schema, handler=handler,
            check_fn=impl.token_configured, requires_env=["SLACK_BOT_TOKEN"], emoji=emoji,
        )
