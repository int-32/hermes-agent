# Native task intake and original-thread delivery

Kanban is the sole current task source. Use the official local operator command,
from the controller's real profile home, once per task:

```sh
hermes kanban source accept --task-id TASK --source-key ACTUAL_NATIVE_KEY \
  --project NATIVE_PROJECT_ID --handoff-path /canonical/root/project/TASK/HANDOFF.md \
  --title 'Task title'
```

Add `--native-id CARD_ID` for an existing card. The source key is selected from
real native routing storage, never assembled from a channel ID. The command
resolves the routing entry and the owning profile's actual session row, checks
owner/guild/profile/parent and canonical key, and pins the source on that card as
an immutable native event. No supplied SID is accepted. Native project ID and
document namespace are distinct metadata. Creation and source binding use one
Kanban writer transaction; retries reuse the card, including after compression.
There is no task-policy whitelist, baseline records, second DB or new task list.
Legacy registration namespace is used only to locate an existing card; its
controller profile never substitutes for the source's actual runtime profile.
The existing tracking-only board must already exist. No agent/run/claim is made.

Routing uses the real producer's resolved `sessions_dir` scope. The persisted key
namespace records the gateway's settled mode, including implicit multiplex; the
operator CLI does not run gateway boot to guess it. The owning profile's current
session row is revalidated at intake, receipt execution, private completion and
return. An unchanged SID cannot replace readable metadata/lifecycle proof.
Normal automatic turn cleanup and proven compression remain continuous; explicit
reset, missing rows, changed ownership and unreadable stores refuse. One card may
have only one stable task/source binding. All queued requests, including old
receipts, respect current pause/closure/project archive; already generated legacy
results may still return through their original private pinned edge.

`hermes kanban source observe --task-id TASK --source-key ACTUAL_NATIVE_KEY
--event /absolute/observation.json` updates that card through native domain APIs.
An observation has `task_id`, positive monotonic `revision`, `kind`, `summary`,
`observed_at` (timezone included), and boolean `needs_user`. Optional worker/owner
fields are audit data, never a replacement source identity. `idle`/`failed` map
to blocked, worker completion maps to review. An `accepted` observation is
refused: use the existing native review/completion contract for acceptance.
Human pause/closure/priority and conflicting-revision guards remain effective.
`source show --task-id TASK` reads the current native source association.

The maintained module entry `python -m scripts.agent_task_intake accept ...`
(or `observe`/`show`) delegates to that same official command implementation.
It does not prepare or call the retired registry write chain. Historical
`prepare`/`apply` policy/records mode is retained solely for deterministic offline
replay; native-mode use refuses. Legacy `kanban registry register/sync` also
refuses native writes. Old records/policy files remain historical evidence.
The service reads card source events per request, so adding a task after code
load needs no task-specific config edit/restart. It still verifies the bound
native turn, real profile/home, current platform authorization and canonical
route. Card body/comments/claimed SID never grant rights. Static
`coordination.tasks` entries are ignored for new admission; pre-upgrade in-flight
receipts retain their pinned return edge. Missing/unreadable stores never fall
back to a list. Native private receipts/FIFO remain the receiving/return path;
ordinary intake never sends a Discord message or launches HWB.

## Historical explicit Discord projection receipts

Existing sealed projection packets and delivery cursors remain usable evidence.
The following describes that explicit delivery adapter, not a current task
admission requirement. Its per-packet destination checks do not register tasks.

After success, `projection.json` contains the exact original thread, body and
allowed mention intent. `project(policy, packet, fetch=..., send=...)` is an adapter
seam for the host's existing authorized Discord tools. Obtain applicable send
consent before invoking an outbound adapter. The host must check channel guild and
parent, fetch actual messages including `channel_id`, bot author ID, content and
`mentions`, and return the actual send result. A retry searches for exact readback
first. Only an authoritative `definitely_not_sent: true` permits automatic retry
after a failure; network ambiguity refuses another send until independently
reconciled. Delivery receipts/cursors are projection state, not a task source.
Before calling the outbound adapter, persist the task's pending association with
the original packet and marker. A later revision reuses that original packet for
equivalent observations. A different substantive observation must also wait until
an unresolved pending delivery is reconciled; an older successful cursor cannot
replace it. An unreadable response or absence from the latest 100 messages is not
evidence of non-delivery. No TTL clears this uncertainty. Authoritatively unsent
failures can retry the original packet, or be superseded by a changed observation.

The explicit CLI `--policy POLICY --projection PROJECTION project` uses the shipped
Discord channel-info/fetch tools and official `hermes send`, checks the binding's
`parent`, and reads full matching messages through the native REST helper because
the public summary omits `mentions`. It sends only when explicitly invoked after
applicable consent; `prepare`/`apply` never send. The policy's bot ID must be verified
from the actual bot identity before delivery. This is an edge command, not a new
authentication or secrets store.

Repeated identical input converges on one card, custody annotation and reminder.
Different observations with no changed state/owner/summary/mention intent reuse
the last verified reminder. A later state transition gets a new message. Failed
projection never retries registry or execution. No UI/client notification claim
follows from an API readback, and joining a thread requires separate evidence.

Failure/idle never means done. `blocked` requires an actual user action request;
internal faults use `failed`. Completion stays review. `accepted` requires a
separate controller policy entry `acceptance[digest(event)]` with `reviewer`,
absolute evidence `path`, and byte `sha256`, and source profile distinct from
the owner. This is explicit operator-reviewed independent evidence, not a new
authentication system. The native ledger still preserves human pause/closure,
priority and native completion-contract requirements.

This delivery covers explicit invocation at agent acceptance/delivery through the
profile skill. It is not a global lifecycle hook, does not infer formal acceptance
from every agent start/end, and does not grant a new native coordination identity.
Profile routing/loading, native receiving/first tool, HWB execution, board readback,
Discord membership/delivery and client notification must be reported separately.
