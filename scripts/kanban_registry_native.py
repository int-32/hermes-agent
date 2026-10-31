"""Version 2 registry: native projects and external lifecycle on a tracking board.

The native board is the only task ledger. Intake files are explicit producer
observations; identity, user decisions and snapshots are read back from it.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3

from scripts.kanban_registry_records import (
    AUTHOR, REPO, RegistryError, checked_path, encoded, identity, snapshot, task_key,
)

PROJECT_NAMES = {'casepilot': '小贷项目', 'n-detection': '版权项目',
                 'aiyu-ops': '自动运维', 'system': '总控/系统建设'}
STATUS_MAP = {'queued': 'ready', 'running': 'running', 'review': 'review',
              'waiting_time': 'scheduled', 'waiting_user': 'blocked', 'failed': 'blocked',
              'unknown': 'blocked', 'idle': 'blocked', 'stopped': 'blocked'}
STATUS_EVENTS = {'tracking_status', 'status', 'completed', 'archived', 'unblocked', 'blocked',
                 'promoted', 'promoted_manual', 'scheduled', 'review_requested', 'changes_requested', 'review_reopened',
                 'descendant_invalidated', 'parent_reopened', 'reclaimed'}


def target_config(config):
    mode = config.get('mode')
    if mode == 'offline':
        root = checked_path(Path(config['offline_root']), root=REPO / '.hermes')
        if root == REPO / '.hermes':
            raise RegistryError('dedicated_offline_root_required')
        config.update(offline_root=root, profile_home=root / 'home', kanban_home=root / 'home')
    elif mode == 'native':
        for key in ('profile_home', 'kanban_home', 'dispatcher_home'):
            config[key] = checked_path(Path(config[key]))
        if not re.fullmatch('[a-f0-9]{40}', config.get('runtime_revision', '')):
            raise RegistryError('pinned_compatible_runtime_required')
    else:
        raise RegistryError('invalid_target_mode')
    projects = config.get('projects')
    if not isinstance(projects, dict) or set(projects) != set(PROJECT_NAMES):
        raise RegistryError('four_explicit_project_bindings_required')
    for slug, spec in projects.items():
        if not isinstance(spec, dict) or set(spec) - {'path', 'ref'}:
            raise RegistryError('invalid_project_binding')
        spec['path'] = checked_path(Path(spec['path']))
        if not spec['path'].is_dir():
            raise RegistryError('project_path_unavailable')
    return config


def runtime_ready(config):
    if config['mode'] == 'offline':
        return None
    from gateway.control_socket import identify_gateway
    from gateway.code_skew import current_code_sha
    if current_code_sha() != config["runtime_revision"]:
        raise RegistryError("local_runtime_revision_mismatch")
    identity = identify_gateway(config['dispatcher_home'])
    if not identity or identity.get('code_sha') != config['runtime_revision']:
        raise RegistryError('compatible_live_dispatcher_not_verified')
    return {key: identity.get(key) for key in ('pid', 'start_time', 'code_sha', 'hermes_home')}


@contextlib.contextmanager
def target_scope(config, *, write=False):
    if config['mode'] == 'offline':
        from scripts.kanban_registry_sync import offline_scope
        with offline_scope(config):
            yield
        return
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    if os.environ.get('HERMES_KANBAN_DB'):
        raise RegistryError('inherited_db_pin_refused')
    if write:
        runtime_ready(config)
    desired = {'HERMES_HOME': str(config['profile_home']), 'HERMES_KANBAN_HOME': str(config['kanban_home']),
               'HERMES_KANBAN_BOARD': config['board']}
    previous = {k: os.environ.get(k) for k in desired}
    token = set_hermes_home_override(config['profile_home'])
    os.environ.update(desired)
    try:
        yield
    finally:
        reset_hermes_home_override(token)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def target_path(config):
    path = config['kanban_home'] / 'kanban' / 'boards' / config['board'] / 'kanban.db'
    for suffix in ('', '-wal', '-shm', '.init.lock', '.dispatch.lock'):
        checked_path(Path(str(path) + suffix), root=config['kanban_home'])
    checked_path(path.parent / 'board.json', root=config['kanban_home'])
    return path


def _project(conn, slug, spec):
    from hermes_cli import projects_db as pdb
    project = pdb.get_project(conn, spec.get('ref') or slug)
    if project and (project.archived or project.primary_path != str(spec['path'])):
        raise RegistryError('native_project_binding_changed')
    if not project and spec.get('ref'):
        raise RegistryError('explicit_native_project_missing')
    return project


def projects(config, *, write=False):
    from hermes_cli import projects_db as pdb
    path = checked_path(config['profile_home'] / 'projects.db')
    if not write and not path.exists():
        return dict.fromkeys(PROJECT_NAMES)
    manager = pdb.connect_closing() if write else contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True))
    with manager as conn:
        conn.row_factory = sqlite3.Row
        if not write:
            conn.execute('PRAGMA query_only=ON')
        result = {}
        for slug, spec in config['projects'].items():
            project = _project(conn, slug, spec)
            if project is None and write:
                pid = pdb.create_project(conn, name=PROJECT_NAMES[slug], slug=slug,
                                         primary_path=str(spec['path']), board_slug=config['board'])
                project = pdb.get_project(conn, pid)
            result[slug] = project.id if project else None
        return result


def desired_status(observation):
    state = observation['external_state']
    if state == 'completed':
        return 'done' if observation['goal_status'] == 'accepted' else 'review'
    return STATUS_MAP[state]


def manual_decision(conn, task, previous):
    if task.status in {'done', 'archived'}:
        return True
    if previous is None or previous.get("manual_status_preserved"):
        return True
    if task.status != previous.get('applied_native_status'):
        return True
    for event in conn.execute('SELECT kind, payload FROM task_events WHERE task_id=? AND id>? ORDER BY id',
                              (task.id, previous.get('native_event_id', -1))):
        if event['kind'] not in STATUS_EVENTS:
            continue
        data = json.loads(event['payload'] or '{}')
        if event['kind'] != 'tracking_status' or data.get('actor') != AUTHOR:
            return True
    return False


def plan_row(conn, record, config, index, project_ids, *, now=None):
    from scripts.kanban_registry_sync import latest_snapshot
    key = task_key(record)
    observed = snapshot(record, config, now=now)
    wanted = desired_status(observed)
    row = dict(task_id=record['task_id'], key=key, native_id=None, native_status=None,
               project=record['project'], project_id=project_ids[record['project']],
               desired_native_status=wanted, action='create', observation=observed)
    if key not in index:
        if any(body['identity']['source'] == record['source'] for _, body in index.values()):
            raise RegistryError('source_owned_by_another_task')
        if not observed['admitted']:
            row['action'] = 'blocked_admission'
        return row
    task, body = index[key]
    row.update(native_id=task.id, native_status=task.status, action='sync')
    if body != {'schema': AUTHOR, 'identity': identity(record)} or task.project_id != row['project_id']:
        row['action'] = 'blocked_identity'
        return row
    previous = latest_snapshot(conn, task.id)
    if previous and (observed['revision'] < previous['revision'] or
                     observed['revision'] == previous['revision'] and observed['input_digest'] != previous['input_digest']):
        row['action'] = 'blocked_revision'
        return row
    if manual_decision(conn, task, previous):
        row.update(manual_status_preserved=True, desired_native_status=task.status)
    before = {k: v for k, v in (previous or {}).items() if k not in {'applied_native_status', 'native_event_id', 'manual_status_preserved'}}
    if before == observed and task.status == row['desired_native_status']:
        row['action'] = 'noop'
    return row


def result_for(rows, index, project_ids, *, present, config):
    from scripts.kanban_registry_sync import result_for as legacy_result
    result = legacy_result(rows, index, present=present, config=config)
    result.update(mode=config['mode'], registry_version=2, tracking_only=present,
                  native_lifecycle_sync='native tracking-board columns; external observations never create native runs',
                  projects=project_ids,
                  native_counts={status: sum(row['native_status'] == status for row in rows)
                                 for status in sorted({row['native_status'] for row in rows if row['native_status']})})
    return result


def inspect(config, records, *, now=None):
    from hermes_cli import kanban_db as kb
    from scripts.kanban_registry_sync import native_index
    path = target_path(config)
    if kb.read_board_metadata(config['board']).get('archived'):
        raise RegistryError('native_board_archived')
    project_ids = projects(config)
    if not path.exists():
        return result_for([plan_row(None, r, config, {}, project_ids, now=now) for r in records],
                          {}, project_ids, present=False, config=config)
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        if not kb.board_tracking_only(conn):
            raise RegistryError('native_board_is_not_tracking_only')
        index = native_index(conn)
        rows = [plan_row(conn, r, config, index, project_ids, now=now) for r in records]
        return result_for(rows, index, project_ids, present=True, config=config)


def _write_row(conn, record, row, config):
    from hermes_cli import kanban_db as kb
    if row['action'] == 'noop':
        return
    tid = row['native_id']
    if tid is None:
        tid = kb.create_task(conn, title=record['title'], created_by=AUTHOR,
                             body=encoded({'schema': AUTHOR, 'identity': identity(record)}),
                             initial_status='blocked', idempotency_key=row['key'],
                             workspace_kind='dir', workspace_path=str(config['projects'][record['project']]['path']),
                             project_id=row['project_id'], board=config['board'],
                             tenant=record['profile'], completion_contract='local-only')
    observed = dict(row['observation'])
    if not row.get('manual_status_preserved'):
        evidence = {'goal_status': observed['goal_status'], 'result': 'External goal accepted; approved evidence refs: ' + ', '.join(record['receipts']),
                    'input_digest': observed['input_digest'], 'receipt_versions': observed['receipt_versions']}
        if not kb.observe_tracking_task(conn, tid, row['desired_native_status'], actor=AUTHOR,
                                        evidence=evidence, expected_status=row['native_status']):
            raise RegistryError('native_status_changed')
    if row.get('manual_status_preserved'):
        observed['manual_status_preserved'] = True
    observed['applied_native_status'] = kb.get_task(conn, tid).status
    observed['native_event_id'] = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?', (tid,)).fetchone()[0]
    kb.add_comment(conn, tid, AUTHOR, encoded(observed))


def apply(config, records, *, now=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from scripts.kanban_registry_sync import native_index
    runtime_ready(config)
    before = inspect(config, records, now=now)
    if any(r['action'].startswith('blocked_') for r in before['rows']):
        return dict(before, write_result='blocked_batch')
    path = target_path(config)
    if not path.exists():
        kb.create_board(config['board'], name='统一任务登记', tracking_only=True)
    with kbc.connect_closing(board=config['board']) as conn:
        if not kb.board_tracking_only(conn):
            raise RegistryError('native_board_is_not_tracking_only')
        with kbc.write_txn(conn):
            # Serializing on this canonical board also serializes this registry's
            # project creation. An interrupted task batch leaves no partial cards.
            project_ids = projects(config, write=True)
            index = native_index(conn)
            rows = [plan_row(conn, r, config, index, project_ids, now=now) for r in records]
            if any(r['action'].startswith('blocked_') for r in rows):
                return dict(result_for(rows, index, project_ids, present=True, config=config), write_result='blocked_batch')
            for record, row in zip(records, rows):
                _write_row(conn, record, row, config)
    return inspect(config, records, now=now)
