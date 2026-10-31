# Task handoff check / guarded execution

This is an explicit invocation gate, not an installed hook, dispatcher or command classifier.
The controller still decides whether work is coordination or task execution and selects the
required stage. A PASS certifies the selected evidence stages; it grants no permission to send,
spend, deploy or perform business work. All model/transport positives in the regression are
**offline fixtures**, never real task activation.

## Dependencies and trust

Python 3.11+, Git and the **reviewed, clean, pinned Hermes source and its existing environment**.
There are no new packages. `--local-only` uses stdlib and Git and does not load credentials or
construct a gateway. Fresh Discord GETs use the installed `agent.secret_scope` loader and
`tools.discord_tool` getter/request API under the selected profile scope. A scoped token must
exist; ambient credential fallback is refused. Credentials, message bodies and exception bodies
are never emitted or saved by the checker. No new paid API is used.

Native proof uses the already-loaded `gateway.agent_handoff._service`, the maintained
`coordination_policy` / `native_endpoint` contract and pure `caller_endpoint` / `target_endpoint`
current-source/allowed-edge guards, the service's journal and the selected
endpoint's profile home. **A fresh CLI process cannot reconstruct the live service.** Therefore
standalone `handoff_received` and later stages stay UNVERIFIED, even when a local session index
or HWB owner exists. No constructor, fake caller binding or serialized "collector result" is used
to obtain a PASS. The reviewed installation has no built-in collector RPC or policy reload verb.

A supported host can import `scripts.task_handoff_check` into its existing native process and
`await check_task(Task(...))` or `await guard(Task(...), argv)`. `task_handoff_native.collect` is
also an explicit read-only collector entry. It uses the existing process objects; it does not
start a turn or write a receipt. Do not expose an untrusted service/module injection endpoint.
This repair supplies an opt-in local plugin host; deployment remains a separate controller action.
The trust boundary assumes the native
process, its reviewed code and protected native databases are trusted; this cannot attest a
malicious operator rewriting native SQLite or Python under the same OS identity.

## Fixed-task plugin host (repair)

`plugins/task-handoff-host` uses the existing plugin discovery/reload, hook and supervised-task
contracts. It attaches only to the process's existing native service; it never constructs one.
It requires in-process plugins, a reviewed canonical descriptor and SHA-pinned native/checker
files. Another profile, changed bytes or an already loaded different checker is refused. No
gateway/core file or model tool is added. The pinned native service is an internal seam, so a
different Hermes revision requires review rather than an assumed compatibility promise.

`$PROFILE_HOME/runtime/task-handoff.sock` is a POSIX-only, owner-only (0600) local socket. Its parent
must belong to this OS user and forbid group/world writes. Clients bind a fresh nonce and the
fixed task inputs to each response, and match PID/start time/source SHA/home to the gateway's
ordinary `identify` socket. No saved report is accepted. Transport failure returns UNVERIFIED/3
and the guard does not start a child. Clients must use this reviewed bundle, adding `--native-host`
to the check/exec command below. `--local-only` still forbids Discord reads and cannot pass.

The host exposes only `status` and `check` for the descriptor's one task. The former
`plan` / `activate` writers are retired: both CLI verbs return exit 2 before contacting a host
or reading config, and direct host methods refuse. Do not use an archived six-field plan or
write `agent_handoff.coordination.tasks`; that list no longer admits a task.

Task admission uses the official `hermes kanban source accept` command (see
`agent_task_intake.md`). It resolves the actual persisted SessionStore routing scope and the
owning profile's session row, then binds one native tracking-only card to one task/source.
The collector's live `coordination_policy` derives those card bindings and retains the current
main/edge/owner/profile/home/SID guards. Its read-only check works without a static tasks map.

`check` runs the maintained collector under the owning profile; SQLite and Discord reads run
off the gateway event loop. Receipts, fresh Discord GETs and actual task tool results remain
mandatory. Request/module/service/identity injection is not an API.

Use `python -m scripts.task_handoff_host --home "$PROFILE_HOME" status` from the reviewed bundle
root with the installed source on PYTHONPATH. A new code revision still requires independent
acceptance and an authorized external load; a per-task card binding is not proof of that load.
Unloading cancels the supervised host and removes its owned socket/hook, preserving native
cards and journal history. Do not clear history or restart the gateway as a rollback shortcut.

## Stage contracts

The requested stage includes every preceding stage. Every stage appears in the JSON, even when
it lacks evidence. Higher-stage failures do not change a lower-stage-only decision.

| Stage | Independent observations required |
| --- | --- |
| `thread_ready` | canonical bounded `<root>/<project>/<task>/HANDOFF.md`; exact Discord thread ID/type/parent/guild, explicit unlocked + unarchived metadata, exact user membership, exact message ID/channel/body against the supplied expected scope |
| `handoff_received` | live registered task + main endpoints, native authorization/canonical route/SID checks; exact profile/home/project/document hash/origin/target association; fresh native admission; gateway-authored request envelope in the task session |
| `execution_started` | correlated assistant tool call **and** native tool result, inside that task turn; tools in the main or an unrelated turn do not count |
| `result_routed` | native `target_message_ids` + response, read back at the exact task thread with matching IDs and content; a controller post, return-to-main message or text claiming a request ID is insufficient |

The native journal is opened with SQLite `mode=ro`, `query_only=ON`, a read transaction and exact
`requests` table shape; no recovery constructor runs. Transcript reads are limited to the pinned
request's task SIDs and timestamps (max 2,000 rows / 100 compression segments). Only user envelope
content and tool metadata are selected; controller transcripts and tool result bodies are not
read. SID continuation delegates to `SessionDB(read_only=True).get_compression_tip`; resets,
forks and absent sessions fail. The same SID also requires a readable eligible row; current
profile/key/platform/owner/chat/thread/origin metadata must still match the native endpoint. Missing/unreadable/stale/ambiguous data remains UNVERIFIED.

Receipt freshness defaults to 600 seconds and has a finite 86,400-second upper bound. The checker
requires historical request evidence, not a still-running worker. Completed tasks can pass when
the explicit evidence horizon covers their receipt. Very old receipts require a separately
reviewed freshness policy; this implementation keeps them UNVERIFIED. Split Discord content must
concatenate exactly to the recorded response; transformed/truncated content cannot pass.

## Canonical commands

Set these shell variables to reviewed values (no tokens in arguments):

```sh
CHECK=/absolute/reviewed/bundle/scripts/task_handoff_check.py
PY=/absolute/existing/hermes/environment/bin/python
SOURCE=/absolute/reviewed/hermes-source
SOURCE_SHA=full_40_character_source_commit
DOCROOT=/absolute/canonical/handoffs
PROFILE_HOME=/absolute/canonical/profile-home
```

Replace every capitalized task placeholder with its exact value. `EXPECTED_BODY` is a UTF-8 file
containing the exact previously selected scope message body, with intentional newlines. If it is
unavailable, omit `--message-body-file`; the stage stays UNVERIFIED. Omit `--request-id` only when
no native receipt has been identified; it cannot pass native stages.

```sh
"$PY" "$CHECK" check \
  --task-id TASK_ID --project PROJECT --profile PROFILE --profile-home "$PROFILE_HOME" \
  --handoff-root "$DOCROOT" --document "$DOCROOT/PROJECT/TASK_ID/HANDOFF.md" \
  --main-channel MAIN_ID --thread THREAD_ID --guild GUILD_ID --user USER_ID \
  --message-id SCOPE_MESSAGE_ID --message-body-file "$EXPECTED_BODY" \
  --request-id NATIVE_REQUEST_ID --stage execution_started \
  --evidence-source native --hermes-source "$SOURCE" --source-revision "$SOURCE_SHA"
```

For a no-network inspection, append `--local-only`. It never produces thread readiness PASS.
The actual Flow/Kanban invocations and their exact argv are in this task's `evidence/known-*`.

For the guard, replace `check` with `exec` and append `-- /absolute/program literal_arg ...` to
the **same invocation**. It collects again; never chain an old JSON PASS into a different command.
`guard(Task(...), argv)` provides the same behavior in an already supported native host.

Only a complete PASS starts the downstream program, using a literal argv array and `shell=False`.
There is no force flag, saved-PASS input, arbitrary boolean credential or fixture mode. The caller
chooses the program; explicitly selecting a shell as that program still has that shell's semantics.
The guard checks immediately before execution but is not a transaction spanning mutable Discord,
SQLite and the child process. It does not bind the child process to Discord, move model context,
classify its business meaning or block commands launched outside this entry.

JSON goes to stdout; child output goes to stderr. Codes: 0 = gate PASS/check success, 2 = FAIL,
3 = UNVERIFIED/input/collection error. A started child preserves its exit code; 124 is its timeout,
and signals map to 128+signal. `exec.started`, `timed_out`, `returncode` distinguish child outcome
from gate status. `--exec-timeout` defaults to 3,600 seconds (finite, max 86,400); timeout kills the
direct child using subprocess's standard behavior, not an entire arbitrary descendant tree.

## Regression and deployment boundary

From the source checkout, always use the repository runner:

```sh
HERMES_PYTHON=/absolute/existing/test-environment/bin/python \
  scripts/run_tests.sh --scratch-root .hermes/test-runs --file-retries 0 -j 2 \
  tests/scripts/test_task_handoff_check.py tests/scripts/test_task_handoff_host.py \
  tests/plugins/test_task_handoff_host_plugin.py
python scripts/check
```

Tests use a temporary HOME, the real native SessionDB schema/compression implementation, a fake
transport and an offline native boundary. The task package includes additional tests against the
fixed installed native producers and private receipt API, with simulated model/tool/transport.
Run those from the frozen source snapshot as documented in REPAIR-REPORT.md so real-HOME protection
stays enabled. Fixture PASS does not prove the gateway loaded or executed this task.

After independent acceptance, deploy the two Python files **preserving `scripts/` beneath the
bundle root**, plus this document. The standalone layout is:

```
<approved target>/scripts/task_handoff_check.py
<approved target>/scripts/task_handoff_native.py
<approved target>/scripts/task_handoff_check.md
```

The controller selects and reviews the target. The fixed-task repair additionally delivers
`scripts/task_handoff_host.py`, the plugin and an exact descriptor; see CONTROLLER-REPAIR.md.
No task/receipt is created by installation. Unloading the host preserves any separately approved
native registration and all native history; it does not revoke already accepted work.
