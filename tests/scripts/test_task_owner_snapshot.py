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
    assert code == 2 and result["delta"]["removed_tasks"] == []
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


# Behaviour contracts preserved from the independent 48610f3 review.
@pytest.mark.parametrize('separator', ['\u2028', '\u0085'])
def test_unicode_unchanged_snapshot_obeys_actual_cron_gate(home, separator):
    from cron.scheduler_prompt import _parse_wake_gate
    from cron.scheduler_script import _run_job_script
    with sqlite3.connect(home / 'kanban.db') as db:
        db.execute('UPDATE tasks SET title=?', ('before'+separator+'after',))
    _, first = scan(home)
    previous = home.parent / 'baseline-unicode.json'
    previous.write_text(json.dumps(first), encoding='utf-8')
    wrapper = home / 'scripts/unicode.py'
    wrapper.parent.mkdir()
    from tests.scripts.test_task_owner_snapshot import SCRIPT
    args = [str(SCRIPT), '--home', str(home), '--previous', str(previous), '--now', '1001']
    wrapper.write_text('import sys,runpy\nsys.argv='+repr(args)+'\nrunpy.run_path(sys.argv[0],run_name="__main__")\n', encoding='utf-8')
    import os
    old = os.environ.get('HERMES_HOME')
    os.environ['HERMES_HOME'] = str(home)
    try:
        ok, output = _run_job_script(str(wrapper), interpreter=sys.executable)
    finally:
        if old is None: os.environ.pop('HERMES_HOME', None)
        else: os.environ['HERMES_HOME'] = old
    assert ok, output
    assert json.loads(output)['wakeAgent'] is False
    assert _parse_wake_gate(output) is False, 'actual cron consumer wakes an unchanged successful snapshot'



def test_missing_previous_board_is_not_successful_task_deletion(home):
    other = home / 'kanban/boards/other'
    other.mkdir(parents=True)
    with sqlite3.connect(other / 'kanban.db') as db:
        db.executescript("CREATE TABLE tasks(id TEXT,status TEXT);INSERT INTO tasks VALUES('other','running');")
    _, first = scan(home)
    (home / 'kanban.db').rename(home / 'kanban-unavailable.db')
    code, current = scan(home, first, 1001)
    assert code == 2 and current['errors'], current['delta']['removed_tasks']
    assert current['delta']['removed_tasks'] == []



def test_same_turn_identical_completion_not_reemitted_after_another_done(home):
    a = {'kind':'turn_done','t':'a','subtype':'completed','text':'a'}
    b = {'kind':'turn_done','t':'b','subtype':'failed','text':'b'}
    worker(home, events=[{'kind':'turn_start'}, a, b, a])
    code, current = scan(home)
    assert code == 0
    delta = current['delta']['executors'][0]
    assert delta['omitted_completions'] == 0
    assert len(delta['completions']) == 2
    assert len({row['key'] for row in delta['completions']}) == 2



def test_old_snapshot_and_checkpoint_deadline_compatibility(home):
    _, first = scan(home)
    old = {k:v for k,v in first.items() if k in ('schema','boards','executors','errors','coverage')}
    snapshot = home.parent/'old.json'
    snapshot.write_text(json.dumps(old), encoding='utf-8')
    cp = home.parent/'checkpoint.json'
    cp.write_text(json.dumps({'schema':'task-owner-inspection-checkpoint/v2','observed_at':'1970-01-01T00:16:40+00:00','consumed_fingerprints':{'snapshot_path':str(snapshot)}}),encoding='utf-8')
    code, before = scan(home, now=1001, checkpoint=cp)
    assert code == 0 and not before['wakeAgent'] and before['next_review_at'] == 2800
    code, due = scan(home, before, 2800)
    assert code == 0 and due['wakeAgent'] and 'review_due' in due['reasons']



def test_overflow_is_explicit_observation_not_consumption(home):
    events=[]
    for i in range(4):
        events += [{'kind':'turn_start','text':str(i)},{'kind':'turn_done','text':str(i)}]
    worker(home, events=events)
    code, result=scan(home)
    assert code == 0
    delta=result['delta']['executors'][0]
    assert delta['omitted_completions'] == 2 and len(delta['completions']) == 2
    assert all(x['consumption']==x['delivery']=='UNVERIFIED' for x in delta['completions'])
    code, after=scan(home,result,1001)
    assert code == 0 and not after['wakeAgent']
    assert after['delta']['executors'] == []



@pytest.mark.parametrize('fault',['truncate','replace','partial'])
def test_prior_complete_cursor_survives_each_event_read_failure(home,fault):
    root=worker(home,events=[{'kind':'turn_start'},{'kind':'turn_done','text':'original'}])
    code,first=scan(home)
    assert code==0
    path=root/'events.jsonl'
    cursor=first['executors'][0]['event_cursor']
    if fault=='truncate':
        path.write_bytes(b'')
    elif fault=='replace':
        path.rename(root/'old.jsonl')
        path.write_text(json.dumps({'kind':'turn_done','text':'replacement'})+'\n',encoding='utf-8')
    else:
        with path.open('ab') as stream:
            stream.write(b'{"kind":"turn_done","text":"next"')
    code,result=scan(home,first,1001)
    assert code==2 and result['errors'] and result['wakeAgent']
    assert result['executors'][0]['event_cursor']==cursor
    assert result['delta']['executors'][0]['completions']==[]
    if fault=='partial':
        with path.open('ab') as stream: stream.write(b'}\n')
        code,recovered=scan(home,first,1002)
        assert code==0
        assert recovered['delta']['executors'][0]['completions'][0]['summary']=='next'
        assert recovered['executors'][0]['event_cursor']['offset']==path.stat().st_size



def test_unreadable_existing_board_does_not_emit_removed_tasks(home):
    _,first=scan(home)
    (home/'kanban.db').write_bytes(b'broken')
    code,result=scan(home,first,1001)
    assert code==2 and result['errors']
    assert result['delta']['removed_tasks']==[]



def test_many_unchanged_polls_do_not_push_review_due(home):
    _,current=scan(home)
    deadline=current['next_review_at']
    for now in [1100,1200,1500,2000,2799]:
        code,current=scan(home,current,now)
        assert code==0 and not current['wakeAgent']
        assert current['next_review_at']==deadline
    code,current=scan(home,current,2800)
    assert code==0 and 'review_due' in current['reasons']


def test_missing_board_error_cannot_become_healthy_by_reusing_error_output(home):
    other = home / "kanban/boards/other"
    other.mkdir(parents=True)
    with sqlite3.connect(other / "kanban.db") as db:
        db.executescript("CREATE TABLE tasks(id TEXT,status TEXT);")
    _, baseline = scan(home)
    original = home / "kanban.db"
    unavailable = home / "kanban-unavailable.db"
    original.rename(unavailable)
    code, missing = scan(home, baseline, 1001)
    assert code == 2 and missing["delta"]["removed_tasks"] == []
    assert any(row["reason"] == "previous_board_unavailable" for row in missing["errors"])
    code, still_missing = scan(home, missing, 1002)
    assert code == 2 and still_missing["errors"] and still_missing["wakeAgent"]
    assert still_missing["delta"]["removed_tasks"] == []
    unavailable.rename(original)
    code, restored = scan(home, baseline, 1003)
    assert code == 0 and not restored["errors"]
    assert restored["delta"]["removed_tasks"] == []


def test_successfully_read_board_can_report_actual_row_deletion(home):
    _, baseline = scan(home)
    with sqlite3.connect(home / "kanban.db") as db:
        db.execute("DELETE FROM tasks WHERE id='t1'")
    code, current = scan(home, baseline, 1001)
    assert code == 0 and current["errors"] == []
    assert current["delta"]["removed_tasks"] == [{"database": str(home / "kanban.db"), "id": "t1"}]


def test_same_turn_dedup_retains_bounded_keys_across_observation_cursors(home):
    a = {"kind": "turn_done", "t": "a", "text": "a"}
    b = {"kind": "turn_done", "t": "b", "text": "b"}
    root = worker(home, events=[{"kind": "turn_start"}, a, b])
    _, first = scan(home)
    with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(a) + "\n")
    code, repeat = scan(home, first, 1001)
    assert code == 0
    assert repeat["delta"]["executors"][0]["completions"] == []
    assert repeat["delta"]["executors"][0]["omitted_completions"] == 0
    assert len(repeat["executors"][0]["turn_completion_keys"]) == 2
    with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "turn_start"}) + "\n" + json.dumps(a) + "\n")
    code, next_turn = scan(home, repeat, 1002)
    assert code == 0 and len(next_turn["delta"]["executors"][0]["completions"]) == 1
    assert len(next_turn["executors"][0]["turn_completion_keys"]) == 1


def test_turn_dedup_limit_requires_reconciliation_without_advancing_cursor(home):
    events = [{"kind": "turn_start"}] + [{"kind": "turn_done", "text": str(i)} for i in range(64)]
    root = worker(home, events=events)
    _, first = scan(home)
    assert len(first["executors"][0]["turn_completion_keys"]) == 64
    with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "turn_done", "text": "65th"}) + "\n")
    code, overflow = scan(home, first, 1001)
    assert code == 2 and any(row["reason"] == "completion_window_exceeded" for row in overflow["errors"])
    assert overflow["executors"][0]["event_cursor"] == first["executors"][0]["event_cursor"]
    assert overflow["executors"][0]["turn_completion_keys"] == first["executors"][0]["turn_completion_keys"]
    assert overflow["delta"]["executors"][0]["completions"] == []


# Original successor-review fixtures and behaviour assertions, unchanged.
def append(root, events):
    with (root / 'events.jsonl').open('a', encoding='utf-8') as f:
        for e in events:
            f.write(json.dumps(e) + '\n')



def test_empty_previous_source_is_still_required(home):
    other = home / 'kanban/boards/other'
    other.mkdir(parents=True)
    with sqlite3.connect(other / 'kanban.db') as db:
        db.execute('CREATE TABLE tasks(id TEXT,status TEXT)')
    _, baseline = scan(home)
    (other / 'kanban.db').rename(other / 'away.db')
    code, missing = scan(home, baseline, 1001)
    assert code == 2 and any(e['source'] == str(other / 'kanban.db') and e['reason'] == 'previous_board_unavailable' for e in missing['errors'])
    code, repeated = scan(home, missing, 1002)
    assert code == 2 and repeated['delta']['removed_tasks'] == []
    (other / 'away.db').rename(other / 'kanban.db')
    code, recovered = scan(home, baseline, 1003)
    assert code == 0 and recovered['errors'] == [] and recovered['delta']['removed_tasks'] == []



def test_corrupt_source_error_baseline_cannot_shed_coverage(home):
    other = home / 'kanban/boards/other'
    other.mkdir(parents=True)
    with sqlite3.connect(other / 'kanban.db') as db:
        db.execute('CREATE TABLE tasks(id TEXT,status TEXT)')
    _, baseline = scan(home)
    (home / 'kanban.db').write_bytes(b'broken')
    code, error = scan(home, baseline, 1001)
    assert code == 2 and any(e['reason'] == 'previous_board_unavailable' for e in error['errors'])
    code, again = scan(home, error, 1002)
    assert code == 2 and again['delta']['removed_tasks'] == []



@pytest.mark.platforms('posix')
def test_inventory_failure_does_not_erase_required_sources_on_error_baseline_reuse(home):
    other = home / 'kanban/boards/other'
    other.mkdir(parents=True)
    with sqlite3.connect(other / 'kanban.db') as db:
        db.executescript("CREATE TABLE tasks(id TEXT,status TEXT);INSERT INTO tasks VALUES('other','running')")
    profiles = home / 'profiles'
    profiles.mkdir()
    _, baseline = scan(home)
    assert len(baseline['boards']) == 2
    (home / 'kanban.db').rename(home / 'unavailable.db')
    profiles.chmod(0)
    try:
        code, error = scan(home, baseline, 1001)
        assert code == 2 and error['errors']
    finally:
        profiles.chmod(0o700)
    code, again = scan(home, error, 1002)
    # Same deliberate wrong-baseline resilience claimed by R1, but with an
    # earlier inventory exception before check_board_coverage is reached.
    observation = {'first_errors': error['errors'], 'first_boards': error['boards'],
                   'next_exit': code, 'next_errors': again['errors'],
                   'next_boards': [b['database'] for b in again['boards']],
                   'next_removed_tasks': again['delta']['removed_tasks'], 'next_reasons': again['reasons']}
    print(json.dumps(observation, sort_keys=True))
    assert code == 2 and any(e['source'] == str(home / 'kanban.db') and e['reason'] == 'previous_board_unavailable' for e in again['errors']), observation



def test_64_keys_allow_repeat_and_reset_on_new_turn(home):
    dones = [{'kind': 'turn_done', 'text': str(i)} for i in range(64)]
    root = worker(home, events=[{'kind': 'turn_start'}] + dones)
    code, baseline = scan(home)
    assert code == 0 and len(baseline['executors'][0]['turn_completion_keys']) == 64
    append(root, [dones[0]])
    code, repeat = scan(home, baseline, 1001)
    assert code == 0 and repeat['delta']['executors'][0]['completions'] == []
    assert repeat['delta']['executors'][0]['omitted_completions'] == 0
    append(root, [{'kind': 'turn_start'}, dones[0]])
    code, reset = scan(home, repeat, 1002)
    assert code == 0 and len(reset['executors'][0]['turn_completion_keys']) == 1
    assert len(reset['delta']['executors'][0]['completions']) == 1
    assert reset['executors'][0]['turn_key'] != baseline['executors'][0]['turn_key']



@pytest.mark.parametrize('fault', ['partial', 'malformed', 'overflow'])
def test_read_failure_rolls_back_all_dedup_state_and_retries(home, fault):
    dones = [{'kind': 'turn_done', 'text': str(i)} for i in range(64 if fault == 'overflow' else 2)]
    root = worker(home, events=[{'kind': 'turn_start'}] + dones)
    code, baseline = scan(home)
    assert code == 0
    old = baseline['executors'][0]
    path = root / 'events.jsonl'
    raw = path.read_bytes()
    if fault == 'overflow':
        append(root, [{'kind': 'turn_done', 'text': '65th'}])
    else:
        append(root, [{'kind': 'turn_start'}, {'kind': 'turn_done', 'text': 'tentative'}])
        with path.open('ab') as f:
            f.write(b'{"kind":"turn_done"' if fault == 'partial' else b'{bad}\n')
    code, error = scan(home, baseline, 1001)
    assert code == 2 and error['errors'] and error['wakeAgent']
    current = error['executors'][0]
    for key in ('event_cursor', 'turn_key', 'turn_completion_keys', 'last_completion'):
        assert current[key] == old[key], key
    assert current['result_state'] == 'UNVERIFIED'
    assert error['delta']['executors'][0]['completions'] == []
    # Restore only unread suffix; the retained complete-line cursor/anchor is valid.
    with path.open('r+b') as f:
        f.truncate(len(raw))
    append(root, [{'kind': 'turn_start'}, {'kind': 'turn_done', 'text': 'recovered'}])
    code, recovered = scan(home, error, 1002)
    assert code == 0 and len(recovered['executors'][0]['turn_completion_keys']) == 1
    assert recovered['delta']['executors'][0]['completions'][0]['summary'] == 'recovered'



def test_legacy_snapshot_seeds_last_ended_completion(home):
    done = {'kind': 'turn_done', 'text': 'same'}
    root = worker(home, events=[{'kind': 'turn_start'}, done])
    _, baseline = scan(home)
    baseline['executors'][0].pop('turn_completion_keys')
    append(root, [done])
    code, repeat = scan(home, baseline, 1001)
    assert code == 0 and repeat['delta']['executors'][0]['completions'] == []
    assert len(repeat['executors'][0]['turn_completion_keys']) == 1


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("fault", ["home", "boards_inventory", "later_profile_inventory", "board_stat",
                                  "board_read", "worker_inventory", "worker_meta", "worker_events"])
def test_coverage_finalization_survives_all_collection_error_paths(home, fault):
    other = home / "kanban/boards/other"
    other.mkdir(parents=True)
    peer = home / "profiles/peer"
    (peer / "kanban/boards").mkdir(parents=True)
    for database in (other / "kanban.db", peer / "kanban.db"):
        with sqlite3.connect(database) as db:
            db.executescript("CREATE TABLE tasks(id TEXT,status TEXT);INSERT INTO tasks VALUES('other','running')")
    root = worker(home)
    code, baseline = scan(home)
    assert code == 0 and len(baseline["boards"]) == 3
    known = {board["database"] for board in baseline["boards"]}
    (home / "kanban.db").rename(home / "unavailable.db")
    restricted = {"boards_inventory": home / "kanban/boards", "later_profile_inventory": peer / "kanban/boards",
                  "worker_inventory": home / "workers"}.get(fault)
    path = {"board_read": other / "kanban.db", "worker_meta": root / "meta.json",
            "worker_events": root / "events.jsonl"}.get(fault)
    rewritten = (path, path.read_bytes()) if path else None
    moved_home = home.parent / "unavailable-home"
    if fault == "home":
        home.rename(moved_home)
    if fault == "board_stat":
        (other / "kanban.db").rename(other / "saved.db")
        (other / "kanban.db").symlink_to("kanban.db")
    if restricted:
        restricted.chmod(0)
    if rewritten:
        rewritten[0].write_bytes(b"{bad}\n")
    try:
        code, current = scan(home, baseline, 1001)
        assert code == 2 and current["errors"] and current["wakeAgent"]
        observed = {board["database"] for board in current["boards"]}
        unknown = {row["source"] for row in current["errors"] if row["reason"] == "previous_board_unavailable"}
        assert observed | unknown == known
        assert current["delta"]["removed_tasks"] == []
    finally:
        if restricted:
            restricted.chmod(0o700)
        if rewritten:
            rewritten[0].write_bytes(rewritten[1])
        if fault == "home":
            moved_home.rename(home)
        if fault == "board_stat":
            (other / "kanban.db").unlink()
            (other / "saved.db").rename(other / "kanban.db")
    for now in (1002, 1003):
        code, current = scan(home, current, now)
        assert code == 2 and current["wakeAgent"] and current["delta"]["removed_tasks"] == []
        assert {row["source"] for row in current["errors"] if row["reason"] == "previous_board_unavailable"} == {str(home / "kanban.db")}
    assert scan(home, baseline, 1004)[0] == 2
    (home / "unavailable.db").rename(home / "kanban.db")
    code, recovered = scan(home, current, 1005)
    assert code == 0 and recovered["errors"] == [] and recovered["delta"]["removed_tasks"] == []
    code, stable = scan(home, recovered, 1006)
    assert code == 0 and stable["wakeAgent"] is False
