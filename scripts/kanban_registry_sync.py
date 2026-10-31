"""Register external work on an isolated native board, without dispatching it.

Run as ``hermes kanban registry --config CONFIG --records INTAKE plan``.
Version 1 retains the original offline hold; version 2 targets tracking boards.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
from pathlib import Path

from scripts.kanban_registry_records import (
    AUTHOR, PREFIX, RegistryError, checked_path, encoded, identity, load_config,
    read_json, snapshot, task_key, validate_record,
)


def load_records(path: Path, config: dict) -> list[dict]:
    raw = read_json(path)
    if not isinstance(raw, list) or not raw:
        raise RegistryError("explicit_nonempty_intake_required")
    unique, sources = {}, {}
    for item in raw:
        record = validate_record(item, config)
        key = task_key(record)
        source = encoded(record["source"])
        if key in unique and unique[key] != record:
            raise RegistryError("conflicting_intake")
        if source in sources and sources[source] != key:
            raise RegistryError("source_owned_by_another_task")
        unique[key], sources[source] = record, key
    return list(unique.values())


@contextlib.contextmanager
def offline_scope(config: dict):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root = checked_path(config["offline_root"])
    home = checked_path(root / "home", root=root)
    if any((home / name).exists() for name in ("config.yaml", ".env", "gateway.pid", "cron/jobs.json", "workers.md")):
        raise RegistryError("non_disposable_home_refused")
    desired = {"HERMES_HOME": str(home), "HERMES_KANBAN_HOME": str(home),
               "HERMES_KANBAN_BOARD": config["board"]}
    if os.environ.get("HERMES_KANBAN_DB"):
        raise RegistryError("inherited_db_pin_refused")
    # Worker ownership and delegated-child fences are never cleared here.
    previous = {key: os.environ.get(key) for key in desired}
    token = set_hermes_home_override(home)
    os.environ.update(desired)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def db_path(config: dict) -> Path:
    home = config["offline_root"] / "home"
    path = home / "kanban" / "boards" / config["board"] / "kanban.db"
    checked_path(path, root=config["offline_root"])
    for suffix in ("-wal", "-shm", ".init.lock", ".dispatch.lock"):
        checked_path(Path(str(path) + suffix), root=config["offline_root"])
    checked_path(path.parent / "board.json", root=config["offline_root"])
    return path


def native_index(conn) -> dict:
    from hermes_cli import kanban_db as kb

    index, sources = {}, {}
    for task in kb.list_tasks(conn, include_archived=True):
        key = task.idempotency_key or ""
        if not key.startswith(PREFIX):
            continue
        if key in index:
            raise RegistryError("duplicate_native_identity")
        try:
            body = json.loads(task.body or "")
            source = encoded(body["identity"]["source"])
        except (ValueError, KeyError, TypeError):
            raise RegistryError("native_identity_modified") from None
        if source in sources:
            raise RegistryError("duplicate_native_source")
        index[key], sources[source] = (task, body), key
    return index


def latest_snapshot(conn, task_id: str) -> dict | None:
    from hermes_cli import kanban_db as kb

    comments = [c for c in kb.list_comments(conn, task_id) if c.author == AUTHOR]
    if not comments:
        return None
    try:
        result = json.loads(comments[-1].body)
        if result["schema"] != AUTHOR:
            raise RegistryError("snapshot_modified")
        return result
    except (ValueError, KeyError, TypeError):
        raise RegistryError("snapshot_modified") from None


def native_hold_intact(conn, task) -> bool:
    from hermes_cli import kanban_db as kb

    events = [e for e in kb.list_events(conn, task.id) if e.kind in {"blocked", "unblocked"}]
    return bool(events and events[-1].kind == "blocked") and not any(
        (task.claim_lock, task.current_run_id, task.worker_pid)
    )


def plan_row(conn, record: dict, config: dict, index: dict, *, now=None) -> dict:
    key = task_key(record)
    observation = snapshot(record, config, now=now)
    row = {"task_id": record["task_id"], "key": key, "native_id": None,
           "native_status": None, "action": "create", "observation": observation}
    if key not in index:
        source = record["source"]
        if any(body["identity"]["source"] == source for _, body in index.values()):
            raise RegistryError("source_owned_by_another_task")
        if not observation["admitted"]:
            row["action"] = "blocked_admission"
        return row
    task, body = index[key]
    row.update(native_id=task.id, native_status=task.status, action="append")
    if body != {"schema": AUTHOR, "identity": identity(record)}:
        row["action"] = "blocked_identity"
    previous = latest_snapshot(conn, task.id) if row["action"] == "append" else None
    if previous and observation["revision"] < previous["revision"] or previous and observation["revision"] == previous["revision"] and observation["input_digest"] != previous["input_digest"]:
        row["action"] = "blocked_revision"
    elif previous == observation:
        row["action"] = "noop"
    # A user may close/archive/reprioritize a card. Observations remain comments;
    # the registry never changes native lifecycle, title, priority or assignee.
    if task.status in {"ready", "todo", "running", "review", "triage"}:
        row["action"] = "blocked_native_dispatchable"
    if task.status == "blocked" and not native_hold_intact(conn, task):
        row["action"] = "blocked_native_hold_lost"
    if row["action"].startswith("blocked_"):
        observation.update(external_state="unknown", goal_status="unverified")
        observation["reasons"] = sorted(set(observation["reasons"]) | {row["action"]})
    return row


def result_for(rows: list[dict], index: dict, *, present: bool, config: dict) -> dict:
    counts = {kind: sum(r["observation"]["external_state"] == kind for r in rows)
              for kind in sorted({r["observation"]["external_state"] for r in rows})}
    errors = sum(r["action"].startswith("blocked_") or bool(r["observation"]["reasons"]) for r in rows)
    requested = {r["key"] for r in rows}
    return {"schema": AUTHOR, "mode": "offline", "board": config["board"],
            "native_board_present": present, "native_managed_count": len(index) if present else None,
            "intake_count": len(rows), "outside_intake_count": len(set(index) - requested) if present else None,
            "external_counts": counts, "unverified_or_blocked_count": errors,
            "native_lifecycle_sync": "unsupported; observations only; no automatic completion",
            "coverage": "explicit approved intake only; other profiles/chats/patrol runs not enumerated",
            "rows": rows}


def inspect(config: dict, records: list[dict], *, now=None) -> dict:
    path = db_path(config)
    if not path.exists():
        rows = [plan_row(None, r, config, {}, now=now) for r in records]
        return result_for(rows, {}, present=False, config=config)
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        index = native_index(conn)
        rows = [plan_row(conn, r, config, index, now=now) for r in records]
        return result_for(rows, index, present=True, config=config)


def apply(config: dict, records: list[dict], *, now=None) -> dict:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    preflight = inspect(config, records, now=now)
    if any(r["action"].startswith("blocked_") for r in preflight["rows"]):
        preflight["write_result"] = "blocked_batch"
        return preflight
    kb.create_board(config["board"], name="External task registry (offline)")
    with kbc.connect_closing(board=config["board"]) as conn:
        # Native create idempotency alone races and ignores archived cards.
        # The official composable transaction serializes lookup + create +
        # receipt comments, including crash recovery and multiple CLI writers.
        with kbc.write_txn(conn):
            index = native_index(conn)
            rows = [plan_row(conn, r, config, index, now=now) for r in records]
            if any(r["action"].startswith("blocked_") for r in rows):
                result = result_for(rows, index, present=True, config=config)
                result["write_result"] = "blocked_batch"
                return result
            for record, row in zip(records, rows):
                if row["action"] == "noop":
                    continue
                tid = row["native_id"]
                if tid is None:
                    tid = kb.create_task(
                        conn, title=record["title"], created_by=AUTHOR,
                        body=encoded({"schema": AUTHOR, "identity": identity(record)}),
                        initial_status="blocked", idempotency_key=row["key"],
                        workspace_kind="scratch", project_id="", board=config["board"],
                        tenant=record["profile"], completion_contract="local-only",
                    )
                kb.add_comment(conn, tid, AUTHOR, encoded(row["observation"]))
    return inspect(config, records, now=now)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--records", type=Path, required=True)
    result.add_argument("command", choices=("plan", "register", "sync", "verify"))
    return result


def main(argv=None) -> int:
    config = {}
    try:
        args = parser().parse_args(argv)
        config = load_config(args.config)
        records = load_records(args.records, config)
        write = args.command in {"register", "sync"}
        if write and config.get('mode') == 'native':
            raise RegistryError('legacy_registry_writes_retired_use_kanban_source')
        if config.get('version') == 2:
            from scripts import kanban_registry_native as native
            with native.target_scope(config, write=write):
                result = (native.apply if write else native.inspect)(config, records)
                if args.command == "verify" and config["mode"] == "native":
                    result["dispatcher_identity"] = native.runtime_ready(config)
        else:
            with offline_scope(config):
                result = (apply if write else inspect)(config, records)
        print(encoded(result))
        incomplete = result["unverified_or_blocked_count"] or (args.command == "verify" and any(r["action"] != "noop" for r in result["rows"]))
        return 3 if incomplete else 0
    except (RegistryError, KeyError, TypeError, OSError, ValueError, sqlite3.Error) as exc:
        code = str(exc) if isinstance(exc, RegistryError) else "invalid_or_unavailable_input"
        print(encoded({"error": code, "mode": config.get("mode", "unresolved")}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
