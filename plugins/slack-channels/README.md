# slack-channels

Tools in the `slack_admin` toolset for readable task channels and messaging agent bots:

| Tool | What it does |
|---|---|
| `slack_create_code_channel` | Calls `agents.conversations.create` with the origin message, then posts the first message in the new channel |
| `slack_rename_channel` | Calls `conversations.rename`, then reads the name back with `conversations.info` |
| `slack_post_message` | Posts in a C/G channel and thread, with optional user mention prefixes; returns message timestamps and a best-effort permalink |
| `slack_list_agents` | Lists active bot users across all `users.list` pages, excluding this bot and deleted users |

## Slack app setup

Bot token scopes:

| Scope | Needed for |
|---|---|
| `code_channels:manage` | Creating a Code Channel |
| `chat:write` | Posting the first message and `slack_post_message` |
| `users:read` | `slack_list_agents` |
| `channels:manage` | Renaming public channels |
| `groups:write` | Renaming private channels |
| `channels:read` | Reading back the name of public channels |
| `groups:read` | Reading back the name of private channels |

Without the two read scopes a rename can still go through, but it returns `rename_readback_failed`, because
the result cannot be confirmed. A created channel also comes back with `name: null` and `name_unavailable`.

Code Channels also need this in the app manifest:

```yaml
features:
  code_channels:
    enabled: true
oauth_config:
  scopes:
    bot:
      - code_channels:manage
```

To apply this:

1. Open <https://api.slack.com/apps>, select the app, and edit its **App Manifest** (or **OAuth & Permissions** for scopes).
2. Save, then click **Reinstall to Workspace** and approve. New scopes do nothing until the app is reinstalled.
3. Reinstalling can issue a new bot token. If it does, update `SLACK_BOT_TOKEN` in the profile's `.env`.

## Enabling the tools in a profile

These steps are per profile:

1. Enable the plugin. Bundled general plugins are opt-in:
   ```bash
   hermes -p <profile> plugins enable slack-channels
   ```
2. Add `slack_admin` to the profile's platform toolsets in its `config.yaml`:
   ```yaml
   platform_toolsets:
     slack:
       - hermes-slack
       - slack_admin
   ```
3. Restart that profile's gateway. Tools load at startup, and a running conversation keeps its toolset.

Listing it is what guarantees the toolset is on. Plugin toolsets default to on, so after step 1 `slack_admin` is
also on for every other platform, including the CLI, unless that platform's `known_plugin_toolsets` entry in
`config.yaml` already names it (`hermes tools` writes that list on save). Use `hermes tools` to turn it off
where you don't want it.

`SLACK_BOT_TOKEN` must hold one bot token. It is read on every call from the active profile's scope, and the
tools are hidden while it is unset. A comma-separated multi-workspace value returns `multiple_workspace_tokens`,
and tokens in `slack_tokens.json` are not used.

## Calling from a Slack conversation

In a Slack conversation, only `name` and `session_id` are needed:

```json
{"name": "fix-login", "session_id": "t_16a59075"}
```

The new channel links to the current channel and the message that triggered the turn, or the current thread
if that turn has no message ID. The result has `origin_source: "session"` and shows the values used in
`origin_channel_id` and `origin_message_ts`.

| Origin arguments | Result |
|---|---|
| Both passed | Used as given, with `origin_source: "arguments"` |
| Neither passed, in a Slack conversation | Inferred, with `origin_source: "session"` |
| Only one passed | `invalid_arguments`: they must be passed together |
| Neither passed, and no Slack channel and message are available | `origin_unknown` |

`origin_unknown` also applies outside Slack and to a Slack slash command run outside a thread, which has no
message to link to.

For `slack_post_message`, `text` is required. Omitted `channel_id` comes from the current Slack channel;
omitted `thread_ts` comes from the current Slack thread, then the triggering message ID. Defaults are only
available in a Slack session. Pass `thread_ts: "none"` explicitly to post top-level; without a timestamp
the tool refuses to send. Only C/G channel targets are accepted: bot-to-bot DMs (U/D targets) are unsupported.
`mention_user_ids` is an array of IDs matching `^U[A-Z0-9]{8,}$`; mentions are prefixed in array order.

## Results

`slack_list_agents` returns a JSON array of `{user_id, name, real_name, app_id}` on success. Other successes
return JSON with `ok`. All failures add `ok:false`, `error` (the Slack code when Slack gave one), `message` and
`action`, plus `needed`, `provided`, `arg` or `retry_after_seconds` when they apply.

| Result | Meaning |
|---|---|
| `error: feature_disabled` | Code Channels are not enabled for this app or workspace; see the setup above |
| `error: missing_scope` | `needed` and `provided` show what Slack reported; `action` names the scope to add |
| `partial: true`, `channel_created: true` | The channel exists and `channel_id` is valid, but the first message failed |
| `channel_may_exist: true` | Slack reported a failure that can still leave a channel behind |
| `error: rename_readback_failed` | Slack accepted the rename but the read-back failed; `slack_error` has the reason |
| `error: rename_readback_mismatch` | Slack accepted the rename but the name read back differs; `name` has what was read |

## Behaviour to know about

- **Repeating a create posts again.** With the same `session_id`, Slack returns the existing channel and does
  not say whether it is new, so `initial_message` is posted again. Only repeat a call after a failure.
- **Privacy is inherited.** `is_private` is sent only when the caller sets it.
- **Slack may rename a Code Channel.** It titles channels from the origin message, so the name can change later.
- **Rename permissions are strict.** Slack allows only the channel's creator, a Workspace Admin or a Channel
  Manager. Other channels return `not_authorized`, even with the right scopes.
- **Anyone who can talk to the bot can reach these tools** once `slack_admin` is enabled for that platform.
