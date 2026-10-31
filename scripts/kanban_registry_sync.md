> Historical import/replay interface. Native `register`/`sync` writes are retired;
> use `hermes kanban source accept/observe` for current intake. Native `plan` and
> `verify` remain read-only reconciliation of historical files to existing cards.
> The offline examples below preserve reproducible migration evidence; they are
> not a second current task ledger or mandatory admission step. No old files are
> deleted or old completed cards reopened by this transition.

# Native external task registry (version 2)

`hermes kanban registry --config /absolute/policy.json --records /absolute/intake.json plan`
accepts `plan`, `register`, `sync`, `verify`. The module entry remains supported.
These are deterministic domain operations; no provider calls or task decomposition.
Only explicit approved intake enters the board. No chat crawling or implicit task admission.

## Target and native project bindings

Use the record and evidence schema below, with `version: 2`, and exactly four
project bindings. Each path must already be an absolute canonical directory:

```json
{
  "version": 2,
  "mode": "offline",
  "offline_root": "/absolute/checkout/.hermes/registry-fixture",
  "profile": "default",
  "board": "registry",
  "source_root": "/absolute/approved-evidence",
  "max_age_seconds": 3600,
  "projects": {
    "casepilot": {"path": "/absolute/casepilot"},
    "n-detection": {"path": "/absolute/n-detection"},
    "aiyu-ops": {"path": "/absolute/aiyu-ops-agent"},
    "system": {"path": "/absolute/hermes-agent"}
  },
  "evidence": {}
}
```

Production policy replaces `mode`/`offline_root` with `mode: native`, explicit
`profile_home`, `kanban_home`, `dispatcher_home`, and `runtime_revision` (full
reviewed candidate commit). Native writes require both local source revision
and the live dispatcher's official control-socket identity to match that pin.
This checks actual compatible execution control, not a coordination receipt.
It does not install or restart that runtime. All other consumers must use the
same candidate; an old dispatcher ignores the new board policy. Deployment must
verify that condition before any real lifecycle columns are registered.

Project ids come from native `projects.db`. New bindings use the four slugs and
names 小贷项目 / 版权项目 / 自动运维 / 总控/系统建设. To reuse an existing native
project under another slug, supply its explicit `ref` id or slug alongside its
exact `path`. An archived project or changed primary path is refused. Project
metadata can survive a later interrupted task transaction; retry reuses it.
The registry is not a distributed transaction across the two databases.
`Task.project_id` is the authoritative relation. Filter with native
`hermes kanban --board registry list --project p_ID --json`, Dashboard Project
selector, or `GET /api/plugins/kanban/board?board=registry&project_id=p_ID`.

## Non-execution and lifecycle

A new registry board is created with native `tracking_only` policy in its own
SQLite DB. CLI creation is `hermes kanban boards create SLUG --tracking-only`.
It can change only on an empty board, under the dispatcher tick lock and SQLite
writer lock. JSON metadata cannot override it. No config reload or assignee
convention is needed. Ordinary boards retain their existing execution behavior.
Claims/review claims, full dispatch ticks, specify/decompose and legacy inflight
run backfills honor the policy. Manual review/completion never synthesizes an
attempt on a tracking board. External project directories are not cleaned up.
A missing/invalid policy fails closed for execution consumers.

External running → native running; review → review; queued → ready;
waiting_time → scheduled; waiting_user/failed/idle/stopped/unknown → blocked.
Completed → review until independently evidenced goal acceptance, then done.
Development, merge, deployment evidence remain separate in native comments.
No external observation satisfies a persisted native PR completion contract.
No native run/claim/model output is fabricated for these columns.

The native ledger owns identity, title, priority, closure and manual reopen.
Manual status events remain authoritative across later source revisions.
A snapshot checkpoint records that hold; user title/priority edits never get
copied back from intake. Observation comments retain changed/stale/missing
source reasons. `verify` covers only its explicit intake and reports unknown
coverage rather than claiming every conversation was imported.

`plan`/`verify` are read-only and do not initialize missing databases. All batch
identity/revision/admission checks precede writes. A task plus its receipt is
atomic in the board transaction; concurrent registry writers converge on the
same native id. Repeated workers/request ids remain observations of one task.
The native CLI's delegated-child and owning-worker guards stay active.
Exit codes are unchanged: 0 verified for explicit intake; 2 invalid/refused;
3 unknown/stale/conflicting observations or verify still needing sync. A sync
may persist unknown observations and return 3; inspect `write_result`.

## Deployment boundary

Build the native SPA with `npm ci --workspace web --include-workspace-root
--ignore-scripts --no-audit --no-fund`, then `npm run build -w web`; output is
`hermes_cli/web_dist` (not `web/dist`). The Kanban plugin's shipped IIFE is served
from `plugins/kanban/dashboard/dist` and needs no separate frontend build.

Use native `dashboard.basic_auth` with scrypt `password_hash`; persist the generated
signing secret via the official profile `.env` writer as
`HERMES_DASHBOARD_BASIC_AUTH_SECRET`, never in YAML. The candidate
`python -m scripts.kanban_registry_dashboard` prepares the native settings,
`dashboard.public_url: https://kanban.aiyu-ai.com`, and bundled `basic`/Kanban
plugin enablement through the official writers. Run from the candidate checkout
with other config writers quiescent; observed config changes are refused.
Bind Dashboard only to `127.0.0.1:9119`. A non-loopback public_url engages
native auth even on a loopback backend; never remove it to make a proxy work.
The reviewed deployment package restricts external HTTP to the read-only board,
auth and static routes. Native terminal, configuration and other admin APIs are
not exposed. Password authentication does not grant a separate native protocol
identity or permission to send messages.

A fresh `/api/auth/ws-ticket` is required for every native event WebSocket
connection. Preserve Host, HTTPS scheme, Origin, Upgrade, query/cursor; never
cache tickets or auth responses. Native CSRF/SameSite/CORS and WS host/origin
guards remain enabled. Nginx additionally requires the exact HTTPS Origin for
state-changing auth requests. A tunnel outage is unavailable data, not a done
or stopped task. Reconnect reuses cursor with a newly minted ticket; a full board
read reconciles missed events. Offline browser sizing is not phone 4G proof.

## Version 1 compatibility and evidence schema

The following original offline-only interface is retained unchanged for existing
artifacts. Its fixed blocked hold is not the version 2 business implementation.

# Offline external task registration

This adapter adds tracking records through the native Kanban domain API. It does
not replace HWB, native Ops, or human execution. The only writable destination in
this delivery is a named board beneath `<this checkout>/.hermes/<dedicated run>/home`.
There is no production override. Run from this checkout with its PM test Python.

## Commands

```sh
.venv/bin/python -m scripts.kanban_registry_sync --config /absolute/config.json --records /absolute/records.json plan
.venv/bin/python -m scripts.kanban_registry_sync --config /absolute/config.json --records /absolute/records.json register
.venv/bin/python -m scripts.kanban_registry_sync --config /absolute/config.json --records /absolute/records.json sync
.venv/bin/python -m scripts.kanban_registry_sync --config /absolute/config.json --records /absolute/records.json verify
```

`plan` and `verify` use read-only SQLite plus the native query API. They never
initialize an absent board; its managed count is `null`, not zero. `register` and
`sync` perform the same idempotent batch upsert: create a held card if admitted,
then append changed observation snapshots as native comments. They serialize the
native lookup, creation and snapshot using the official composable write transaction.
A failed/interrupted transaction rolls back card and comment together. A retry
checks archived cards too; an archived or completed card is never recreated.

Exit codes: `0` = the requested operation is verified for the explicit intake;
`2` = invalid/unavailable input or a safety/permission refusal; `3` = observations
are unknown, stale, conflicting or blocked, or `verify` finds work still pending.
An exit of `3` can accompany successfully persisted unknown observations. Inspect
the JSON output. `write_result=blocked_batch` means the whole observation batch
was refused. Revisions describe source observations, never native run numbers.
No provider or network calls, shell execution, notifications, cron jobs or
subscriptions are created by this adapter.

## Inputs and trust

Use two explicitly reviewed JSON files. Config is operator policy; records are a
fixed intake, not a second task ledger. Never generate claims from arbitrary text.
The controller must attest each receipt's meaning before listing its claim in the
config. SHA-256 proves bytes, not semantic correctness or an authenticated author.

Example config (all paths absolute except receipt paths):

```json
{
  "version": 1,
  "mode": "offline",
  "offline_root": "/absolute/checkout/.hermes/registry-example",
  "source_root": "/absolute/sanitized-receipts",
  "profile": "default",
  "board": "registry",
  "max_age_seconds": 3600,
  "evidence": {
    "handoff-v1": {
      "path": "HANDOFF.md",
      "sha256": "<64 lowercase hex digits computed from the approved file>",
      "profile": "default",
      "task_id": "task-example",
      "observed_at": "2026-10-30T05:00:00Z",
      "claims": ["admission:task-example", "external:waiting_user"]
    }
  }
}
```

Create the dedicated offline root before use. A root outside this checkout's
`.hermes`, the `.hermes` root itself, the default board, symlinks, hardlinked input
files, traversal and inherited `HERMES_KANBAN_DB` pins are refused. The home cannot
contain a real config, credentials, gateway PID, jobs file or worker registry.
The native config reader may create its empty home directory skeleton and SOUL;
this does not create a gateway or a schedule. Existing native worker/delegate
permission fences remain active. One CLI process owns one explicit profile scope;
this standalone script is not a thread-safe service API.

Example records file:

```json
[{
  "profile": "default",
  "task_id": "task-example",
  "project": "hermes",
  "title": "Original task title",
  "goal": "Original approved goal and scope",
  "acceptance": "Original acceptance criteria",
  "owner": "controller",
  "thread": "100000000000000001",
  "handoff": "handoff-v1",
  "source": {"kind": "development", "id": "original-request-id"},
  "executor_ids": ["original-worker", "second-worker", "native-request-id"],
  "revision": 1,
  "observed_at": "2026-10-30T05:00:00Z",
  "external_state": "waiting_user",
  "goal_status": "unverified",
  "stages": {"development": "unknown", "merge": "unknown", "deployment": "unknown"},
  "receipts": ["handoff-v1"]
}]
```

`profile` is the registration owner's namespace; external executor IDs and `owner`
retain the original HWB / independent Ops / human routing. No native assignee is
assigned. Source kinds are `development`, `ops`, `manual`. The task's original
source stays fixed; later requests and workers join `executor_ids` on the SAME
`task_id` with a higher revision. Every receipt is bound to one task and profile.
The task identity, original scope and source are stored in its native body;
latest observations and history are native comments. Mapping and coverage are
always derived from native readback. No persistent mapping/pool is needed.

The idempotency key hashes `(profile, task_id)`. Identical intake entries and
repeated receipt/executor IDs normalize to one record. Different records for the
same key, a source reused for another task, altered native identity, a decreasing
revision or different input at the same revision are refused. Unknown/stale source
observations retain their reason codes; an empty or unreadable intake is an error.
Removing a task from intake never deletes, closes or hides its native record;
`outside_intake_count` explicitly reports the retained remainder.

Only curated bounded text/IDs enter the board. Raw receipts, errors, worker prompts,
model output and message bodies are not rendered or executed. Markup, control
characters and common token/password/email/IP/phone patterns in rendered fields
are rejected. This is data minimization, not a general PII detector: prepare
sanitized receipts before admission and never point the manifest at customer raw
data or credentials. Only approved `.md` / `.json` / `.txt` receipts are read.

## Observations and native lifecycle

External states are `unknown`, `queued`, `running`, `waiting_user`, `waiting_time`,
`review`, `failed`, `idle`, `stopped`, `completed`. Each asserted state needs its own
`external:<state>` claim. Both the source observation and execution receipt
observation time must be fresh. `observed_at` is a trusted producer's evidence time;
reading/copying an old file must not refresh it. Missing/changed/stale/conflicting
proof produces `unknown`. PID existence, worker idle/stopped and queued requests
cannot prove a goal was achieved.

The three stages each require their own `<stage>:pending|passed|failed` claim.
Goal acceptance/rejection separately requires `goal:accepted|rejected`. A completed
worker observation without acceptance does not complete a goal. Even a fully
accepted snapshot does not call native `complete_task` or synthesize a run.

New cards use **one atomic native `create_task(initial_status="blocked")`**. That
transaction writes the native sticky `blocked` event. Real dispatcher tests cover
already-running service discovery, reconstructed services, default assignment,
manual claims and auto-decomposition. A positive control exercises the actual
`_default_spawn` / `Popen` path with a local executable that cannot call models.
This safety mechanism does not depend on an absent assignee, triage, a private
lock, an idle worker, or default-off configuration.

Native status is the registry hold; external observations are distinct. The
adapter never sets running/review/ready/done, changes title/priority/assignee,
unblocks/reopens a card, or overwrites a user's confirmation/closure. If a user
moves a card into a dispatchable native lane the adapter refuses further writes.
It also refuses a blocked card whose sticky event was lost or that still has
native run/claim/worker ownership; a column label alone is not isolation proof.
**Native dispatch can occur before the next sync after such a manual release.**
The current core has no per-board tracking-only fence spanning those manual
transitions. Therefore ordinary native lane synchronization and production
rollout remain a controller decision, not an implemented promise.

Completion, if later authorized, must use the normal native completion API/CLI
and its current ownership and mandatory PR acceptance gates. This adapter exposes
no completion shortcut. `edit_task(metadata=...)` updates completed-run metadata;
it is not an external execution observation interface.

## Validation and rollback

```sh
HERMES_PYTHON="$PWD/.hermes/regression/.venv/bin/python" scripts/run_tests.sh --scratch-root .hermes/test-runs --file-retries 0 -j 2 tests/scripts/test_kanban_registry_isolation.py tests/scripts/test_kanban_registry_sync.py
.venv/bin/python scripts/check
```

For native worktree tests that create temporary Git repositories, use the normal
runner's default external scratch directory and a self-contained source checkout;
putting those repositories below a protected checkout correctly trips the live
system guard. Keep that guard enabled. Use PM's `dev,test` groups plus `all` for
native dispatcher/gateway regressions (the lean environment lacks aiohttp).

Offline rollback is stop invoking this script and discard ONLY its disposable
root after retaining required evidence. There is no scheduler, deployment,
production migration or second task pool to undo. Do not delete the old pool,
change the old display cron, move existing cards, or release native holds as a
rollback shortcut. Actual deployment/registration/display cutover needs separate
controller review, fixed inputs, official readback and exact Discord readback.
