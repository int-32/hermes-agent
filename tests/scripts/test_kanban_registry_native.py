"""Native projects and truthful columns on a durable non-executing board."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from scripts import kanban_registry_native as native
from scripts.kanban_registry_records import encoded, load_config
from tests.scripts.test_kanban_registry_sync import NOW

pytest_plugins = ["tests.scripts.test_kanban_registry_sync"]


@pytest.fixture
def native_intake(intake, tmp_path):
    _config, record, cp, rp, receipt = intake
    raw = json.loads(cp.read_text())
    raw['version'] = 2
    raw['projects'] = {}
    for slug in native.PROJECT_NAMES:
        path = tmp_path / slug
        path.mkdir()
        raw['projects'][slug] = {'path': str(path)}
    cp.write_text(encoded(raw))
    record['project'] = 'system'
    return load_config(cp), record, cp, rp, receipt


def apply(config, records):
    with native.target_scope(config):
        return native.apply(config, records, now=NOW)


def state(config, record, status, *, accepted=False):
    record['revision'] += 1
    record['external_state'] = status
    record['goal_status'] = 'accepted' if accepted else 'unverified'
    config['evidence']['handoff']['claims'] = ['admission:example', 'external:' + status, 'goal:accepted']
    return apply(config, [record])['rows'][0]


def test_native_columns_projects_retries_and_stage_separation(native_intake):
    config, record, *_ = native_intake
    with native.target_scope(config):
        plan = native.inspect(config, [record], now=NOW)
        assert plan['rows'][0]['desired_native_status'] == 'running'
        assert not (config['profile_home']).exists()
    first = apply(config, [record])
    assert first['rows'][0]['native_status'] == 'running'
    assert len(set(first['projects'].values())) == 4
    assert None not in first['projects'].values()
    assert apply(config, [record]) == first
    tid = first['rows'][0]['native_id']
    for status, expected in [('queued', 'ready'), ('review', 'review'), ('waiting_time', 'scheduled'), ('idle', 'blocked'), ('stopped', 'blocked'), ('running', 'running'), ('completed', 'review')]:
        assert state(config, record, status)['native_status'] == expected
    assert state(config, record, 'completed', accepted=True)['native_status'] == 'done'
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        task = kb.get_task(conn, tid)
        assert task.project_id == first['projects']['system']
        assert [t.id for t in kb.list_tasks(conn, project_id=task.project_id)] == [tid]
        assert not kb.list_tasks(conn, project_id=first['projects']['casepilot'])
        assert kb.list_runs(conn, tid) == []
        assert kb.claim_task(conn, tid) is None
        assert first['rows'][0]['observation']['stages'] == dict.fromkeys(('development', 'merge', 'deployment'), 'unknown')


def test_manual_status_title_priority_and_reopen_survive_sync(native_intake):
    config, record, *_ = native_intake
    tid = apply(config, [record])['rows'][0]['native_id']
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        kb.edit_task(conn, tid, title='Chosen by user', priority=9)
        kb.block_task(conn, tid, reason='User pause')
    assert state(config, record, 'running')['native_status'] == 'blocked'
    assert state(config, record, 'completed', accepted=True)['native_status'] == 'blocked'
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        kb.complete_task(conn, tid, result='User accepted independently')
    assert state(config, record, 'running')['native_status'] == 'done'
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        kb.observe_tracking_task(conn, tid, 'ready', actor='user', evidence={'reason': 'reopened'})
    assert state(config, record, 'completed', accepted=True)['native_status'] == 'ready'
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        task = kb.get_task(conn, tid)
        assert (task.title, task.priority) == ('Chosen by user', 9)
        assert not kb.list_runs(conn, tid)


def test_missing_stale_and_recovery_do_not_invent_completion(native_intake):
    config, record, *_, receipt = native_intake
    tid = apply(config, [record])['rows'][0]['native_id']
    content = receipt.read_bytes()
    receipt.unlink()
    row = state(config, record, 'completed', accepted=True)
    assert row['native_status'] == 'blocked'
    assert 'receipt_missing' in row['observation']['reasons']
    receipt.write_bytes(content)
    assert state(config, record, 'running')['native_status'] == 'running'
    with native.target_scope(config):
        result = native.apply(config, [record], now=NOW + 3601)
    assert result['rows'][0]['native_status'] == 'blocked'
    assert result['rows'][0]['native_id'] == tid


def test_atomic_retry_after_interruption_and_parallel_idempotency(native_intake, monkeypatch):
    config, record, *_ = native_intake
    original = kb.add_comment
    def fail(*args, **kwargs):
        raise OSError('fixture interruption')
    with monkeypatch.context() as m:
        m.setattr(kb, 'add_comment', fail)
        with pytest.raises(OSError, match='interruption'):
            apply(config, [record])
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        assert kb.list_tasks(conn) == []
    # Native CLI scopes are process-scoped. Threads here use one bound scope;
    # each real DB connection competes on the same SQLite transaction.
    with native.target_scope(config), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: native.apply(config, [copy.deepcopy(record)], now=NOW), range(2)))
    assert len({r['rows'][0]['native_id'] for r in results}) == 1
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        assert len(kb.list_tasks(conn)) == len(kb.list_comments(conn, results[0]['rows'][0]['native_id'])) == 1
    assert kb.add_comment is original


def test_four_native_project_cards_and_cli_registry_entry(native_intake, capsys):
    import argparse
    from datetime import UTC, datetime
    from hermes_cli import kanban as cli
    from hermes_cli.kanban_parser import build_parser
    config, record, cp, rp, _ = native_intake
    raw = json.loads(cp.read_text())
    stamp = datetime.now(UTC).isoformat()
    records = []
    raw['evidence'] = {}
    for slug in native.PROJECT_NAMES:
        item = copy.deepcopy(record)
        item.update(task_id=slug, project=slug, source={'kind': 'manual', 'id': slug}, handoff=slug, receipts=[slug], observed_at=stamp)
        records.append(item)
        raw['evidence'][slug] = dict(config['evidence']['handoff'], task_id=slug, observed_at=stamp, claims=['admission:' + slug, 'external:running'])
        raw['evidence'][slug].pop('resolved')
    cp.write_text(encoded(raw))
    rp.write_text(encoded(records))
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest='command'))
    for command in ('plan', 'register', 'sync', 'verify'):
        args = parser.parse_args(['kanban', 'registry', '--config', str(cp), '--records', str(rp), command])
        assert cli.kanban_command(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert len(result['rows']) == 4
    config = load_config(cp)
    with native.target_scope(config):
        for pid in result['projects'].values():
            listed = json.loads(cli.run_slash(f'list --project {pid} --json'))
            assert len(listed) == 1
            assert listed[0]['project_id'] == pid


def test_native_target_requires_local_and_live_matching_revision(native_intake, monkeypatch):
    from gateway import code_skew, control_socket
    config, *_ = native_intake
    config.update(mode='native', runtime_revision='a' * 40, dispatcher_home=config['profile_home'])
    monkeypatch.setattr(code_skew, 'current_code_sha', lambda: 'b' * 40)
    with pytest.raises(ValueError, match='local_runtime_revision_mismatch'):
        native.runtime_ready(config)
    monkeypatch.setattr(code_skew, 'current_code_sha', lambda: 'a' * 40)
    monkeypatch.setattr(control_socket, 'identify_gateway', lambda home: {'code_sha': 'b' * 40})
    with pytest.raises(ValueError, match='compatible_live_dispatcher_not_verified'):
        native.runtime_ready(config)
    monkeypatch.setattr(control_socket, 'identify_gateway', lambda home: {'code_sha': 'a' * 40, 'pid': 123})
    assert native.runtime_ready(config)['pid'] == 123


def test_registry_does_not_resurrect_archived_board(native_intake):
    config, record, *_ = native_intake
    apply(config, [record])
    with native.target_scope(config):
        kb.remove_board('registry', archive=True)
        with pytest.raises(ValueError, match='native_board_archived'):
            native.apply(config, [record], now=NOW)
        assert not native.target_path(config).exists()
