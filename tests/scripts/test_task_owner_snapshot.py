"""Offline collector contracts; exercise the CLI with real SQLite and HWB files."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/task_owner_snapshot.py"


@pytest.fixture
def home(tmp_path):
    path = tmp_path / ".hermes"
    path.mkdir()
    with sqlite3.connect(path / "kanban.db") as conn:
        conn.executescript("""
            CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT, title TEXT, body TEXT);
            CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, body TEXT);
            INSERT INTO tasks VALUES('t1','running','Example','{}');
        """)
    return path


def worker(home, status="idle", events=None):
    root = home / "workers" / "worker-a"
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta.json").write_text(json.dumps({"id": "worker-a", "status": status}))
    (root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in (events or [])))
    return root


def scan(home, previous=None, now=1000, checkpoint=None):
    args = [sys.executable, str(SCRIPT), "--home", str(home), "--now", str(now)]
    if previous is not None:
        path = home.parent / "previous.json"
        path.write_text(json.dumps(previous))
        args += ["--previous", str(path)]
    if checkpoint:
        args += ["--checkpoint", str(checkpoint)]
    proc = subprocess.run(args, env=dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home)),
                          capture_output=True, text=True, timeout=15, check=False)
    return proc.returncode, json.loads(proc.stdout)


def test_stopped_completion_is_visible_and_observation_is_not_receipt(home):
    worker(home, "stopped", [{"t": "2026-11-01", "kind": "turn_done", "subtype": "success", "text": "done"}])
    code, result = scan(home)
    assert code == 0
    assert result["executors"][0]["status"] == "stopped"
    completion = result["delta"]["executors"][0]["completions"][0]
    assert completion["consumption"] == completion["delivery"] == "UNVERIFIED"
    assert completion["next_action"] == "reconcile_with_original_task_owner"
    assert result["wakeAgent"] is True
    code, again = scan(home, result, 1001)
    assert code == 0 and again["wakeAgent"] is False
    assert again["delta"]["executors"] == []


def test_review_deadline_survives_unchanged_scans_and_fires(home):
    _, first = scan(home)
    _, second = scan(home, first, 2000)
    assert second["wakeAgent"] is False
    assert second["next_review_at"] == first["next_review_at"]
    _, due = scan(home, second, 2800)
    assert due["wakeAgent"] is True and "review_due" in due["reasons"]


def test_long_test_idle_and_no_log_are_never_blockage(home):
    worker(home, "running")
    _, first = scan(home)
    _, due = scan(home, first, 5000)
    assert due["executors"][0]["age_is_not_a_blockage_verdict"] is True
    assert due["delta"]["executors"] == []
    assert "review_due" in due["reasons"]
    assert "blocked" not in due["reasons"]


def test_completed_card_and_bounded_history_delta_without_replay(home):
    _, first = scan(home)
    with sqlite3.connect(home / "kanban.db") as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
        conn.executemany("INSERT INTO task_comments(task_id,body) VALUES('t1',?)",
                         [("text" * 1000,) for _ in range(10)])
    code, result = scan(home, first)
    assert code == 0 and result["boards"][0]["tasks"][0]["status"] == "done"
    summary = result["delta"]["tasks"][0]
    assert summary["phase"] == "done" and len(summary["history_tail"]) == 2
    assert len(json.dumps(summary)) < 3000
    assert summary["unique_wait"] == "UNVERIFIED"
    _, again = scan(home, result, 1001)
    assert again["delta"]["tasks"] == []


@pytest.mark.parametrize("fault", ["database", "worker", "event", "checkpoint"])
def test_read_failure_is_nonzero_and_never_unchanged(home, fault):
    previous = None
    if fault == "database":
        (home / "kanban.db").write_bytes(b"not sqlite")
    elif fault == "worker":
        root = worker(home)
        (root / "meta.json").write_text("{")
    elif fault == "event":
        root = worker(home)
        (root / "events.jsonl").write_text("{bad}\n")
    else:
        previous = {"schema": "unexpected"}
    code, result = scan(home, previous)
    assert code == 2 and result["errors"] and result["wakeAgent"] is True
    assert "read_error" in result["reasons"]


def test_event_backlog_is_bounded_and_resumes_without_losing_completion(home):
    events = [{"kind": "tool", "input": "x" * 1000} for _ in range(100)]
    events.append({"kind": "turn_done", "subtype": "completed", "text": "result"})
    worker(home, events=events)
    _, first = scan(home)
    assert first["executors"][0]["event_cursor"]["offset"] <= 65536
    assert first["executors"][0]["has_more"] is True
    assert first["delta"]["executors"][0]["completions"] == []
    _, second = scan(home, first, 1001)
    assert second["executors"][0]["has_more"] is False
    assert len(second["delta"]["executors"][0]["completions"]) == 1


def test_duplicate_done_does_not_claim_a_second_result(home):
    event = {"kind": "turn_done", "t": "same", "subtype": "completed", "text": "result"}
    root = worker(home, events=[event])
    _, first = scan(home)
    with (root / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    _, second = scan(home, first, 1001)
    assert second["delta"]["executors"][0]["completions"] == []


def test_rotation_partial_line_and_missing_result_require_reconciliation(home):
    root = worker(home, events=[{"kind": "turn_done", "text": "result"}])
    _, first = scan(home)
    (root / "events.jsonl").unlink()
    (root / "events.jsonl").write_text('{"kind":"turn_done"')
    code, result = scan(home, first, 1001)
    assert code == 2
    assert result["executors"][0]["result_state"] == "UNVERIFIED"
    assert result["delta"]["executors"][0]["completions"] == []


def test_existing_checkpoint_v2_is_observation_only_and_input_is_unchanged(home):
    _, first = scan(home)
    prior = home.parent / "snapshot.json"
    prior.write_text(json.dumps(first))
    cp = home.parent / "checkpoint.json"
    cp.write_text(json.dumps({"schema": "task-owner-inspection-checkpoint/v2",
                              "observed_at": "1970-01-01T00:16:40+00:00",
                              "consumed_fingerprints": {"snapshot_path": str(prior)}}))
    raw = cp.read_bytes()
    code, second = scan(home, now=1001, checkpoint=cp)
    assert code == 0 and second["wakeAgent"] is False
    assert cp.read_bytes() == raw


def test_disappeared_inventory_and_future_clock_are_visible(home):
    root = worker(home)
    _, first = scan(home)
    (home / "kanban.db").unlink()
    (root / "meta.json").unlink()
    code, result = scan(home, first, 999)
    assert code == 2 and result["delta"]["removed_tasks"]
    assert result["delta"]["removed_executors"] == []
    assert any(item["source"].endswith("worker-a/meta.json") for item in result["errors"])


def test_home_missing_cannot_report_empty_healthy_coverage(tmp_path):
    code, result = scan(tmp_path / "absent")
    assert code == 2 and result["errors"]


def test_actual_collector_output_reaches_existing_cron_gate(home, monkeypatch):
    from cron.scheduler_prompt import _parse_wake_gate
    from cron.scheduler_script import _run_job_script

    _, first = scan(home)
    previous = home.parent / "baseline.json"
    previous.write_text(json.dumps(first))
    wrapper = home / "scripts/collect.py"
    wrapper.parent.mkdir()
    argv = [str(SCRIPT), "--home", str(home), "--previous", str(previous), "--now", "1001"]
    wrapper.write_text("import runpy,sys\nsys.argv=" + repr(argv) +
                       "\nrunpy.run_path(sys.argv[0],run_name='__main__')\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    ok, output = _run_job_script(str(wrapper), interpreter=sys.executable)
    assert ok, output
    assert _parse_wake_gate(output) is False
    (home / "kanban.db").write_bytes(b"broken")
    ok, output = _run_job_script(str(wrapper), interpreter=sys.executable)
    assert ok is False and "read_error" in output


def test_real_canonical_board_markdown_identity_and_no_writes(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / "native-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    kb.create_board("registry")
    with kbc.connect_closing(board="registry") as conn:
        task_id = kb.create_task(conn, title="Actual schema", body="# Markdown task")
        kb.add_comment(conn, task_id, "owner", "Await independent Ops; no executor wake")
    database = home / "kanban/boards/registry/kanban.db"
    before = database.read_bytes()
    code, result = scan(home)
    assert code == 0 and result["delta"]["tasks"][0]["id"] == task_id
    assert database.read_bytes() == before


@pytest.mark.parametrize("events", ["partial", "oversized", "malformed"])
def test_fault_recovery_retries_unadvanced_cursor(home, events):
    root = worker(home)
    data = {"partial": '{"kind":"turn_done"', "oversized": "x" * 70000,
            "malformed": "{bad}\n"}[events]
    (root / "events.jsonl").write_text(data)
    code, broken = scan(home)
    assert code == 2
    (root / "events.jsonl").write_text(json.dumps({"kind": "turn_done", "text": "recovered"}) + "\n")
    code, recovered = scan(home, broken, 1001)
    assert code == 0 and recovered["wakeAgent"] is True
    assert len(recovered["delta"]["executors"][0]["completions"]) == 1


def test_database_busy_is_explicit_timeout_without_checkpoint_promotion(home):
    lock = sqlite3.connect(home / "kanban.db")
    try:
        lock.execute("BEGIN EXCLUSIVE")
        code, result = scan(home)
        assert code == 2 and result["errors"][0]["error"] == "OperationalError"
    finally:
        lock.rollback()
        lock.close()
    assert scan(home)[0] == 0


@pytest.mark.platforms("posix")
def test_unreadable_inventory_is_not_reported_as_empty(home):
    worker(home)
    directory = home / "workers"
    directory.chmod(0)
    try:
        code, result = scan(home)
        assert code == 2 and result["errors"]
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize("subtype", ["completed", "failed", "interrupted"])
def test_execution_end_is_not_owner_consumption_or_delivery(home, subtype):
    worker(home, "stopped", [{"kind": "turn_done", "subtype": subtype, "text": "result"}])
    code, result = scan(home)
    assert code == 0
    done = result["delta"]["executors"][0]["completions"][0]
    assert done["subtype"] == subtype
    assert done["consumption"] == done["delivery"] == "UNVERIFIED"
    assert result["executors"][0]["result_state"] == "execution_ended"
