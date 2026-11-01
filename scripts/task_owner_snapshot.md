# Task owner observation collector

`task_owner_snapshot.py` extends the existing maintenance
`task-owner-snapshot.py`; it does not introduce another task registry.
Kanban remains the task source. This is a source candidate, not an installed
cron binding. The existing scheduler's last-line `wakeAgent` gate is reused.

## Read-only invocation

```sh
python scripts/task_owner_snapshot.py --home /absolute/isolated/home
python scripts/task_owner_snapshot.py --home /absolute/isolated/home --previous /absolute/observed-snapshot.json
python scripts/task_owner_snapshot.py --home /absolute/isolated/home --checkpoint /absolute/checkpoint.json
python scripts/task_owner_snapshot.py --home /absolute/isolated/home --checkpoint /absolute/checkpoint.json --snapshot-output /absolute/new-observation.json
```

`--home` is explicit; profile discovery is limited to its `profiles` directory.
`--previous` accepts the existing `task-owner-observation/v1` snapshot.
`--checkpoint` instead reads the existing inspection checkpoint v2's
`consumed_fingerprints.snapshot_path`. Neither input is written. `--now`
overrides the clock in offline replays. Invalid CLI arguments exit 2 on stderr.

Stdout is one ASCII-escaped JSON object: original board/worker fingerprints plus `delta`,
`errors`, `reasons`, `wakeAgent`, `observed_at`, and `next_review_at`.
Exit 0 means collection succeeded, **not** task success or delivery.
Exit 2 with `read_error` means collection/checkpoint/coverage could not be
established. Do not promote that output as a consumed baseline.
Unicode text is preserved after JSON decoding; ASCII escaping keeps Unicode line
separators inside JSON strings compatible with the existing cron last-line gate.
Fingerprint serialization is unchanged.

### Optional artifact and cron projection

Without `--snapshot-output`, stdout and exit behavior stay as above. With it,
the **same full observation** is saved at the explicit output path, and stdout
becomes one ASCII JSON line for the existing cron gate. No consumer is changed:
only a successful script with `wakeAgent=false` skips the agent.

The projection contains `wakeAgent`, reasons, coverage counts/scope, errors,
delta, observation/review times, and `snapshot_artifact` (resolved absolute path,
SHA256 of the exact file bytes, byte count, `observation_only=true`). It excludes
the full board/worker inventory. Four delta lists (`tasks`, `executors`,
`removed_tasks`, `removed_executors`) **share 20 entries**, errors return at most
8 entries, and the whole ASCII stdout line including its newline is at most
16 KiB. Selection follows the existing list order, errors first; entries that
do not fit the byte budget are omitted intact. Per-list `omitted` and
`omitted_errors` count every excluded entry. The complete details remain in
the artifact; this is a bound on model context, not collection CPU/memory.

`requires_reconciliation=true` means errors, any omitted entry, an existing
per-worker `omitted_completions`, or an event backlog needs attention. It does
not claim the other observations were consumed. Neither the projection nor
the candidate artifact is an ACK or an automatically promoted baseline.
Reconcile omissions from the artifact and original evidence before the original
owner advances its existing inspection checkpoint. A projection is not accepted
by `--previous`; the full artifact is still observation schema v1.

The existing `utils.atomic_write_text` writes the artifact (0600, temp file,
file fsync, replace, best-effort directory fsync); new mode needs the existing
Hermes source/runtime imports available. The caller owns the output location;
missing parents are created through the helper. Output paths resolving to
`--previous`, `--checkpoint`, or its referenced baseline are refused, including
relative paths, symlinks, directory aliases and existing hardlinks. These checks
also protect this invocation's observation inputs: discovered
Kanban databases (including SQLite's reserved WAL/SHM/journal paths), worker
metadata and event streams. Each DB registration includes both its logical path
and its resolved DB path, with companions derived from **each filename**. The
same resolved object returned by registration supplies SQLite's read-only URI;
file symlinks cannot hide companions under the target DB's different basename.
This also applies before reading failed/missing DBs, to prior/error sources and
to discovered inventory candidates even when a sibling stops collection.
Resolution failures enter the existing errors and return nonzero; an unresolved
identity is never treated as safe publication. A call-local set records paths before I/O, including
failed reads and missing candidates. Inventory checks finish the same directory's
stat pass to register directory/unknown candidates before rethrowing its first
error; ordinary artifact files are excluded. Prior board/worker observations and source
errors retain known paths when inventory disappears. The set is not saved, and
an error snapshot remains unsuitable as a healthy/consumed baseline. Both worker
paths are registered before metadata parsing. No second scan or task registry is
added; unrelated artifact paths inside the explicit home remain valid outputs.
Resolve and identity checks assume caller-owned stable inputs/directories; this is not a
hostile concurrent filesystem transaction. The shared writer retains its existing
platform/bind-mount fallbacks; no new crash or exactly-once guarantee is added.

Input-read failure returns exit 2 and **writes no artifact**. Collection failure
may save a fresh diagnostic observation but still returns exit 2. Output
validation/write/import/reference-budget failure returns exit 2 with
`snapshot_write_error`, `wakeAgent=true`, and `snapshot_artifact=null`; it never
references an old output as success. Existing output can be replaced on a
successful save. A failed save can leave the old file (or an unacknowledged new
file if failure followed replacement); only this invocation's successful return
and matching byte hash identify a published observation. No checkpoint or native
receipt is written. The projection's 16 KiB bound does not lift any downstream
native request content limit; that separate integration is not implemented here.

The full object is machine observation evidence; only its `delta`, reasons,
coverage and gate belong in a model prompt. Keep snapshot files under the
existing inspection's evidence/checkpoint mechanism. There is no output
writer, task list, timer, dispatch loop or new database here.

## Contracts and bounds

- Unchanged, successful observations before the saved review deadline return
  `wakeAgent=false`. An unchanged scan preserves that deadline; a due review
  returns true. The interval remains the task owner's 30 minutes.
- Changed cards alone obtain history text: latest two entries across detected
  comment/event tables, maximum 240 characters per selected field. Inventory
  and full-history fingerprints are still read to detect changes/deletions;
  SQLite may scan/sort history. This is a context bound, not a CPU-time bound.
- Every database named by the previous observation must be read successfully.
  A missing/unreadable previous database is `previous_board_unavailable`, exit 2,
  not removal of its tasks; that coverage error persists if its output is reused.
  One finalization step performs this comparison after every collection path,
  including early inventory errors and later board/worker errors. It derives
  expected sources from the existing snapshot and coverage errors, with no other
  source list. A successfully reread source clears its coverage error; unrelated
  errors are not carried forward as permanent failures.
  Only a successfully read database can produce per-task removals. After recovery,
  compare against the last successfully examined baseline.
- `done`/`archived` cards and stopped HWB observations remain visible. Age,
  idle status and absent logs never decide blockage or acceptance.
- Per worker, read at most 64 KiB from the previous complete-line cursor,
  returning at most two completion summaries of 600 characters each.
  `omitted_completions` and `has_more` are explicit: reconcile overflow from
  the evidence before acknowledging the whole observation. Total inventory
  and number of changed tasks/workers are not capped.
- The cursor pins file identity plus an anchor before the offset. Replacement,
  truncation, malformed/oversized/partial lines and missing previously seen
  streams produce errors. The saved cursor is not advanced on a failed read.
  Complete a partial append and retry the same baseline; on log replacement
  the original owner must reconcile generations before choosing a new baseline.
- Completion keys combine the observed turn start and completion content. The
  existing worker observation retains at most 64 distinct keys for its current
  turn, resetting on `turn_start`. Within that window, adjacent and nonadjacent
  identical completions are suppressed, including across observation cursors.
  A 65th distinct completion without a new start is `completion_window_exceeded`,
  exit 2: retain the prior cursor/keys and let the original owner reconcile.
  Old snapshots without this field can seed only the last completion of an ended turn;
  older nonadjacent repeats and lost-checkpoint history require reconciliation.
  This is bounded observation memory, not a permanent deduplication/ACK ledger;
  it provides no exactly-once consumption or delivery guarantee.

`turn_done` proves only a worker execution boundary, including failed or
interrupted turns. Each completion explicitly reports owner consumption and
platform delivery as `UNVERIFIED`. The upstream HWB event has no native request
ID; do not manufacture one or infer a receipt from a report file. Native
`scripts/task_handoff_native.py` already checks trusted requests/results under
the real service, and should be reused once the owning Task Agent supplies
the supported result-to-request association.

Card-body owner/thread values remain untrusted lookup hints. Summary
`unique_wait` and `wait_owner` remain `UNVERIFIED` where there is no authoritative
structured field; free-form comments never authorize a route or executor wake.

## Integration and recovery

The original task owner owns promotion of successfully examined observation
fingerprints. Saving stdout or moving a cursor is not owner processing,
acceptance, a private ACK or platform delivery. Response loss requires reading
the same native request/receipt; it does not authorize re-sending work.

No active job, installed collector, original Kanban worktree or checkpoint is
changed by this candidate. At reviewed rollout, replace the old collector
call with a thin invocation of this version, then retire that old call; do not
run both indefinitely. The actual job wrapper, profile bindings and runtime
loading require independent Ops evidence through the task's owning main.

## No-argument cron wrapper

`scripts/task_owner_gate.py` is the maintained entry template for an existing
Agent job's `script` field. Copy it unchanged under that profile's `scripts/`
with an adjacent, same-stem `.json` file containing five absolute path bindings:
`home`, `collector`, `checkpoint`, `evidence_directory`, `source_root`. Machine
paths belong in that private delivery file, not in the public source template.
The collector must resolve under the bound home's `scripts/`; the evidence
parent must already exist. The wrapper allocates one unique `evidence-*` directory
with `snapshot.json` per invocation and retains it, including on a skipped Agent
run. Retention remains the original owner's responsibility; preserve referenced
and unconsumed candidates until processing/recovery completes.

`source_root` binds one approved, complete Hermes source root. The wrapper
prepends it and verifies the imported `utils` file's location before invoking
the collector. This is needed for the bare cron fallback, which does not add a
managed source bootstrap; `atomic_write_text` also imports `hermes_constants`
lazily. The selected interpreter must provide that source's existing dependencies
(including `hermes_yaml`/ruamel). A source path alone does not supply dependencies
or establish the installed interpreter. No PM discovery/activation or environment
installation is added by the wrapper. The normal managed cron bootstrap remains
supported. Missing imports or other preparation failures are explicit stderr/exit2;
the collector has not started in that case.

The wrapper accepts no arguments. It passes only the existing home/checkpoint/
snapshot-output arguments to **one in-process** collector invocation, preserving
its stdout and `SystemExit`. It never writes the v2 checkpoint, promotes a candidate,
or acknowledges work. No generation index, second task store, schedule or shared
consumer change is involved. `processing_prompt` and `job_payloads` render the
private processing instruction and single-job forward/rollback tool arguments
offline; they do not apply them. Rollback requires the actual original prompt.
Use a relative script name for the existing cronjob update API. Clear both
monitor sources with empty strings, keep `no_agent=false`, and preserve schedule,
provider/model and delivery by omitting them from the field delta. Runtime path
resolution still enforces the profile scripts boundary.

The Agent must consume the cached prerun result rather than executing a collector
again, even if `context_from=self` contains an older workflow. Verify artifact
path/hash/bytes and reconcile the **full** evidence before advancing the original
checkpoint's consumed reference, preserving its other fields. Nonzero, unknown,
omission/backlog or unfinished processing does not advance a successful baseline.
No native ACK is inferred from that update. The fixed private package and its
single-job paused/drained switch remain separately reviewed operations; nothing
in rendering a package installs files or edits a job. The existing 1800-second
review deadline remains, so an unchanged 30-minute natural fire may still wake
for `review_due`.
