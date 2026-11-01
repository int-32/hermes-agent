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
