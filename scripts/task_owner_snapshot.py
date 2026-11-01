"""Read-only task-owner collector, derived from the existing maintenance script.

Kanban remains authoritative. Cursors mean observed, never accepted or delivered.
No model, dispatch, PID probe, checkpoint write, or worker lifecycle action.
"""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from os import fstat
from pathlib import Path
import sqlite3
import stat
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCHEMA = "task-owner-observation/v1"
EVENT_BYTES = 65536
REVIEW_SECONDS = 1800
TURN_COMPLETION_LIMIT = 64
PROJECTION_BYTES = 16384
PROJECTION_ITEMS = 20
PROJECTION_ERRORS = 8
SQLITE_INPUT_SUFFIXES = ("", "-wal", "-shm", "-journal")


def encoded(value, *, ensure_ascii=False):
    return json.dumps(value, ensure_ascii=ensure_ascii, sort_keys=True, default=str, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def error(result, source, exc):
    # Exception text can contain task data or SQL values.
    known = {"unsupported_checkpoint_schema", "unsupported_snapshot_schema", "unsupported_board_schema",
             "event_stream_changed", "partial_event", "unsupported_event", "worker_id_mismatch",
             "previous_board_unavailable", "completion_window_exceeded", "snapshot_input_alias",
             "projection_reference_too_large"}
    reason = str(exc) if type(exc) is ValueError and str(exc) in known else type(exc).__name__
    result["errors"].append({"source": str(source), "error": type(exc).__name__, "reason": reason})


def load_previous(previous=None, checkpoint=None, *, protected=None):
    if checkpoint:
        record = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
        if record.get("schema") != "task-owner-inspection-checkpoint/v2":
            raise ValueError("unsupported_checkpoint_schema")
        previous = Path(record["consumed_fingerprints"]["snapshot_path"])
        if protected is not None:
            protected.add(previous)
    if not previous:
        return {}
    data = json.loads(previous.read_text(encoding="utf-8-sig"))
    if data.get("schema") != SCHEMA or not isinstance(data.get("boards"), list) or not isinstance(data.get("executors"), list):
        raise ValueError("unsupported_snapshot_schema")
    task_index(data)
    for item in data["executors"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("unsupported_snapshot_schema")
        cursor = item.get("event_cursor")
        if cursor and (not isinstance(cursor, dict) or type(cursor.get("offset")) is not int
                       or cursor["offset"] < 0 or not isinstance(cursor.get("identity"), list)
                       or not isinstance(cursor.get("anchor"), str)):
            raise ValueError("unsupported_snapshot_schema")
    if checkpoint and "next_review_at" not in data:
        data["next_review_at"] = datetime.fromisoformat(record["observed_at"]).timestamp() + REVIEW_SECONDS
    return data


def task_index(snapshot):
    return {(board["database"], task["id"]): task
            for board in snapshot.get("boards", []) for task in board["tasks"]}


def check_board_coverage(previous, result):
    """Finalize coverage and removals even when collection exited through an error."""
    observed = {board["database"] for board in result["boards"]}
    expected = {board["database"] for board in previous.get("boards", [])}
    # An error output is not a healthy baseline, even if supplied again by a caller.
    expected.update(item["source"] for item in previous.get("errors", [])
                    if item.get("reason") == "previous_board_unavailable")
    for database in sorted(expected - observed):
        error(result, database, ValueError("previous_board_unavailable"))
    removed = set(task_index(previous)) - set(task_index(result))
    result["delta"]["removed_tasks"] = [dict(database=db, id=tid) for db, tid in sorted(removed) if db in observed]


def directories(root, *, protected=None, source_files=(), board_result=None):
    # Path.glob can suppress permission/read errors as if inventory were empty.
    try:
        paths = sorted(root.iterdir())
    except FileNotFoundError:
        return []
    found, failure = [], None
    for path in paths:
        try:
            is_directory = stat.S_ISDIR(path.stat().st_mode)
        except OSError as exc:
            if failure is None:
                failure = exc
            is_directory = None
        # Unknown entries stay protected, but ordinary artifact files are not directories.
        if is_directory is not False and protected is not None:
            protected.update(path / name for name in source_files)
            if board_result is not None:
                protect_board_inputs(path / "kanban.db", protected, board_result)
        if is_directory:
            found.append(path)
    if failure is not None:
        raise failure
    return found


def history(conn, tables, task_id, *, tail=False):
    changes, recent = hashlib.sha256(), []
    for table in sorted(tables):
        if not any(word in table for word in ("comment", "event")):
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        fields = [row[1] for row in conn.execute("PRAGMA table_info(" + quoted + ")")]
        if "task_id" not in fields:
            continue
        order = ",".join('"' + key.replace('"', '""') + '"' for key in sorted(fields))
        changes.update(encoded([table, fields]).encode())
        if not tail:
            for row in conn.execute("SELECT * FROM " + quoted + " WHERE task_id=? ORDER BY " + order, (task_id,)):
                changes.update(encoded(dict(row)).encode())
                changes.update(b"\n")
        if tail:
            sequence = '"id"' if "id" in fields else order
            for row in conn.execute("SELECT * FROM " + quoted + " WHERE task_id=? ORDER BY " + sequence + " DESC LIMIT 2", (task_id,)):
                item = {key: str(row[key])[:240] for key in ("id", "kind", "event_type", "created_at", "body", "payload") if key in fields}
                recent.append(dict(table=table, **item))
    recent.sort(key=lambda item: (item.get("created_at", "").rjust(30, "0"), item.get("id", "").rjust(30, "0"), item["table"]))
    return changes.hexdigest(), recent[-2:]


def collect_board(path, resolved, profile, previous, result):
    with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "tasks" not in tables or not {"id", "status"}.issubset(columns):
            raise ValueError("unsupported_board_schema")
        board, summaries = {"profile_home": profile, "database": str(path), "tasks": []}, []
        for row in conn.execute("SELECT * FROM tasks ORDER BY id"):
            task = dict(row)
            item = {key: task.get(key) for key in ("id", "title", "status", "project_id", "assignee", "session_id", "workspace_path", "last_failure_error")}
            item["card_digest"] = digest(task)
            item["history_digest"], _ = history(conn, tables, task["id"])
            try:
                body = json.loads(task.get("body") or "{}")
            except (ValueError, TypeError):
                body = {}  # Markdown cards are valid; identity hints are optional.
            identity = body.get("identity", {}) if isinstance(body, dict) else {}
            if isinstance(identity, dict):
                item["untrusted_lookup_hints"] = {key: identity.get(key) for key in ("task_id", "thread", "profile", "owner")}
            old = previous.get((str(path), task["id"]), {})
            change = [key for key in ("card_digest", "history_digest") if item[key] != old.get(key)]
            if change:
                _, recent = history(conn, tables, task["id"], tail=True)
                summaries.append({"database": str(path), "id": task["id"], "phase": task["status"],
                                  "changes": change, "unique_wait": "UNVERIFIED", "wait_owner": "UNVERIFIED",
                                  "next_action": "read_original_card_and_owner_checkpoint",
                                  "evidence": str(path) + "#tasks/" + str(task["id"]), "history_tail": recent})
            board["tasks"].append(item)
        result["boards"].append(board)
        result["delta"]["tasks"].extend(summaries)


def anchor(stream, offset):
    stream.seek(max(0, offset - 128))
    return hashlib.sha256(stream.read(min(offset, 128))).hexdigest()


def completion(event, turn_key, evidence):
    return {"key": digest([turn_key, event]), "subtype": str(event.get("subtype", "unknown"))[:80],
            "summary": str(event.get("text", ""))[:600], "consumption": "UNVERIFIED", "delivery": "UNVERIFIED",
            "next_action": "reconcile_with_original_task_owner", "evidence": evidence}


def collect_events(path, old, item):
    """Read one bounded chunk; retain only complete-line observation cursors."""
    completions = []
    with path.open("rb") as stream:
        stat = fstat(stream.fileno())
        identity = [stat.st_dev, stat.st_ino]
        prior = old.get("event_cursor") or {}
        offset = prior.get("offset", 0)
        if prior and (prior.get("identity") != identity or not 0 <= offset <= stat.st_size or anchor(stream, offset) != prior.get("anchor")):
            raise ValueError("event_stream_changed")
        item.update(last_completion=old.get("last_completion"), turn_key=old.get("turn_key"),
                    result_state=old.get("result_state", "UNVERIFIED"))
        keys = old.get("turn_completion_keys")
        if keys is None:
            keys = [old["last_completion"]["key"]] if old.get("last_completion") and old.get("result_state") == "execution_ended" else []
        if not isinstance(keys, list) or len(keys) > TURN_COMPLETION_LIMIT:
            raise ValueError("completion_window_exceeded")
        keys = set(keys)
        stream.seek(offset)
        raw = stream.read(min(EVENT_BYTES, stat.st_size - offset))
        complete = raw[:raw.rfind(b"\n") + 1]
        position = offset
        for line in complete.splitlines(keepends=True):
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                raise ValueError("unsupported_event")
            if event["kind"] == "turn_start":
                item.update(turn_key=digest([identity, position]), result_state="executing")
                keys.clear()
            elif event["kind"] == "turn_done":
                done = completion(event, item["turn_key"], str(path) + "#byte=" + str(position))
                if done["key"] not in keys:
                    if len(keys) >= TURN_COMPLETION_LIMIT:
                        raise ValueError("completion_window_exceeded")
                    completions.append(done)
                    keys.add(done["key"])
                item.update(last_completion=done, result_state="execution_ended")
            position += len(line)
        item["event_cursor"] = {"identity": identity, "offset": position, "anchor": anchor(stream, position)}
        item["turn_completion_keys"] = sorted(keys)
        item["has_more"] = position < stat.st_size
        item["event_size"] = stat.st_size
        after = fstat(stream.fileno())
        if after.st_size < stat.st_size or after.st_size == stat.st_size and after.st_mtime_ns != stat.st_mtime_ns:
            raise ValueError("event_stream_changed")
        if raw and not complete or len(complete) < len(raw) and len(raw) < EVENT_BYTES:
            raise ValueError("partial_event")
    return completions


def collect_workers(home, previous, result, now, protected):
    old_workers = {item["id"]: item for item in previous.get("executors", [])}
    for root in directories(home / "workers", protected=protected, source_files=("meta.json", "events.jsonl")):
        path = root / "meta.json"
        events = root / "events.jsonl"
        # Register both before metadata parsing: a failed meta read must not expose its log.
        protected.update((path, events))
        try:
            raw = path.read_bytes()
            meta = json.loads(raw)
            worker_id = path.parent.name
            if not isinstance(meta, dict) or meta.get("id", worker_id) != worker_id:
                raise ValueError("worker_id_mismatch")
            old = old_workers.get(worker_id, {})
            item = {key: meta.get(key) for key in ("status", "cwd", "thread_id", "session_id")}
            item.update(id=worker_id, pid_hint=meta.get("pid"), metadata_digest=hashlib.sha256(raw).hexdigest(),
                        event_size=None, result_state="UNVERIFIED", has_more=False,
                        age_is_not_a_blockage_verdict=True)
            completions = []
            try:
                if events.exists():
                    completions = collect_events(events, old, item)
                elif old.get("event_cursor"):
                    raise FileNotFoundError("previous event stream missing")
            except (OSError, ValueError, TypeError) as exc:
                item.update(event_cursor=old.get("event_cursor"), last_completion=old.get("last_completion"),
                            turn_key=old.get("turn_key"), turn_completion_keys=old.get("turn_completion_keys"),
                            result_state="UNVERIFIED", event_read_error=True)
                error(result, events, exc)
            stamp = events.stat().st_mtime if events.exists() else path.stat().st_mtime
            item["observation_older_than_30m"] = now - stamp >= REVIEW_SECONDS
            result["executors"].append(item)
            keys = ("metadata_digest", "event_size", "event_cursor", "event_read_error")
            if any(item.get(key) != old.get(key) for key in keys):
                result["delta"]["executors"].append({"id": worker_id, "result_state": item["result_state"],
                    "completions": completions[-2:], "omitted_completions": max(0, len(completions) - 2),
                    "wait_owner": "UNVERIFIED", "next_action": "reconcile_with_original_task_owner",
                    "evidence": str(path.parent)})
        except (OSError, ValueError, TypeError) as exc:
            error(result, path, exc)


def protect_board_inputs(path, protected, result):
    """Register the logical and actual SQLite I/O closure; return the path to open."""
    protected.update(Path(str(path) + suffix) for suffix in SQLITE_INPUT_SUFFIXES)
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        error(result, path, exc)
        return None
    # SQLite derives companions from the filename in its opened URI, not a symlink's name.
    protected.update(Path(str(resolved) + suffix) for suffix in SQLITE_INPUT_SUFFIXES)
    return resolved


def observation_inputs(home, previous, protected, result):
    """Call-local paths, including confirmed sources now absent or hidden by inventory errors."""
    if protected is None:
        protected = set()
    protect_board_inputs(home / "kanban.db", protected, result)
    for board in previous.get("boards", []):
        protect_board_inputs(Path(board["database"]), protected, result)
    for item in previous.get("errors", []):
        source = Path(item["source"])
        if item.get("reason") == "previous_board_unavailable" or source.name == "kanban.db":
            protect_board_inputs(source, protected, result)
        elif source.name in ("meta.json", "events.jsonl") and source.parent.parent.name == "workers":
            protected.update((source.parent / "meta.json", source.parent / "events.jsonl"))
    for item in previous.get("executors", []):
        root = home / "workers" / item["id"]
        protected.update((root / "meta.json", root / "events.jsonl"))
    return protected


def collect(home, previous=None, *, now=None, protected=None):
    now = time.time() if now is None else now
    previous = previous or {}
    result = {"schema": SCHEMA, "boards": [], "executors": [], "errors": [], "observed_at": now,
              "delta": {"tasks": [], "executors": [], "removed_tasks": [], "removed_executors": []}}
    protected = observation_inputs(home, previous, protected, result)
    try:
        if not home.is_dir():
            raise FileNotFoundError("observation home missing")
        old_tasks = task_index(previous)
        homes = [("default", home), *((path.name, path) for path in directories(
            home / "profiles", protected=protected, board_result=result))]
        seen = set()
        for profile, root in homes:
            protect_board_inputs(root / "kanban.db", protected, result)
            candidates = [root / "kanban.db", *(path / "kanban.db" for path in directories(
                root / "kanban" / "boards", protected=protected, board_result=result))]
            for path in candidates:
                resolved = protect_board_inputs(path, protected, result)
                if resolved is None:
                    continue
                try:
                    path.stat()
                except FileNotFoundError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    collect_board(path, resolved, profile, old_tasks, result)
                except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                    error(result, path, exc)
        if not seen:
            error(result, home, FileNotFoundError())
        collect_workers(home, previous, result, now, protected)
        unread = {item["source"] for item in result["errors"]}
        current_workers = {item["id"] for item in result["executors"]}
        result["delta"]["removed_executors"] = sorted(item["id"] for item in previous.get("executors", [])
            if item["id"] not in current_workers and str(home / "workers" / item["id"] / "meta.json") not in unread)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        error(result, home, exc)
    finally:
        check_board_coverage(previous, result)
    deadline = previous.get("next_review_at", now)
    observed = previous.get("observed_at", now)
    if (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
            or not isinstance(observed, (int, float)) or not math.isfinite(observed) or observed > now):
        error(result, "review_clock", ValueError())
        deadline = now
    reasons = []
    if not previous:
        reasons.append("initial_observation")
    if any(result["delta"].values()):
        reasons.append("changed")
    if now >= deadline:
        reasons.append("review_due")
    if any(item.get("has_more") for item in result["executors"]):
        reasons.append("event_backlog")
    if result["errors"] or previous.get("errors"):
        reasons.append("read_error")
    result.update(reasons=reasons, wakeAgent=bool(reasons), next_review_at=now + REVIEW_SECONDS if reasons else deadline,
                  coverage="Existing canonical boards in explicit home and installed profile homes; HWB observations include stopped. Unregistered work, owner consumption, and platform delivery remain UNVERIFIED.",
                  checkpoint_meaning="observation only; never permission, task acceptance, owner consumption, or delivery")
    return result


def projection_fits(value):
    # ASCII JSON: characters equal transport bytes. Reserve newline + a true->false change.
    return len(encoded(value, ensure_ascii=True)) + 2 <= PROJECTION_BYTES


def project_observation(result, artifact=None):
    """Bound model context independently of the complete machine observation."""
    omitted = {key: len(items) for key, items in result["delta"].items()}
    value = {"wakeAgent": result["wakeAgent"], "reasons": result["reasons"],
             "coverage": {"scope": result["coverage"], "boards_read": len(result["boards"]),
                          "workers_observed": len(result["executors"]), "complete": not result["errors"]},
             "errors": [], "omitted_errors": len(result["errors"]),
             "delta": {key: [] for key in omitted}, "omitted": omitted,
             "snapshot_artifact": artifact, "observed_at": result["observed_at"],
             "next_review_at": result["next_review_at"], "checkpoint_meaning": result["checkpoint_meaning"],
             "requires_reconciliation": True}
    if not projection_fits(value):
        raise ValueError("projection_reference_too_large")
    for item in result["errors"][:PROJECTION_ERRORS]:
        value["errors"].append(item)
        value["omitted_errors"] -= 1
        if not projection_fits(value):
            value["errors"].pop()
            value["omitted_errors"] += 1
    kept = 0
    for key, items in result["delta"].items():
        for item in items:
            if kept == PROJECTION_ITEMS:
                break
            value["delta"][key].append(item)
            omitted[key] -= 1
            if projection_fits(value):
                kept += 1
            else:
                value["delta"][key].pop()
                omitted[key] += 1
    value["requires_reconciliation"] = bool(result["errors"] or any(omitted.values())
        or any(item.get("has_more") for item in result["executors"])
        or any(item["omitted_completions"] for item in result["delta"]["executors"]))
    return value


def snapshot_destination(output, protected):
    """Check both path aliases and existing hardlinks before the symlink-following writer."""
    destination = output.resolve()
    # Identify direct collisions before a different, unreadable source can abort identity checks.
    if destination in {source.absolute() for source in protected}:
        raise ValueError("snapshot_input_alias")
    for source in protected:
        if destination == source.resolve():
            raise ValueError("snapshot_input_alias")
        try:
            same = destination.samefile(source)
        except FileNotFoundError:
            same = False
        if same:
            raise ValueError("snapshot_input_alias")
    return destination


def save_observation(output, result, protected):
    from utils import atomic_write_text

    destination = snapshot_destination(output, protected)
    content = encoded(result, ensure_ascii=True) + "\n"
    artifact = {"path": str(destination), "sha256": hashlib.sha256(content.encode("ascii")).hexdigest(),
                "bytes": len(content), "observation_only": True}
    value = project_observation(result, artifact)
    atomic_write_text(destination, content, tmp_prefix=".task-owner-", mode=0o600, fsync_dir=True)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--previous", type=Path)
    group.add_argument("--checkpoint", type=Path)
    parser.add_argument("--snapshot-output", type=Path, help="save full observation here; stdout becomes a bounded cron projection")
    parser.add_argument("--now", type=float, help="explicit clock for offline replay")
    args = parser.parse_args()
    if args.now is not None and not math.isfinite(args.now):
        parser.error("--now must be finite")
    protected = {path for path in (args.previous, args.checkpoint) if path is not None}
    inputs_ok = True
    try:
        previous = load_previous(args.previous, args.checkpoint, protected=protected)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        inputs_ok = False
        result = collect(args.home, now=args.now, protected=protected)
        error(result, args.checkpoint or args.previous, exc)
        result.update(wakeAgent=True, reasons=sorted(set(result["reasons"] + ["read_error"])))
    else:
        result = collect(args.home, previous, now=args.now, protected=protected)
    value = result
    if args.snapshot_output is not None:
        value = project_observation(result)
        if inputs_ok:
            try:
                value = save_observation(args.snapshot_output, result, protected)
            except (OSError, ValueError, RuntimeError, ImportError) as exc:
                error(result, "snapshot_output", exc)
                result.update(wakeAgent=True, reasons=sorted(set(result["reasons"] + ["snapshot_write_error"])))
                value = project_observation(result)
    # The cron gate uses str.splitlines(), including U+0085/U+2028 as boundaries.
    print(encoded(value, ensure_ascii=True))
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
