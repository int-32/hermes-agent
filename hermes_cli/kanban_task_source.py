"""Native card provenance. One card event replaces external task admission lists.

Only the local operator entry writes this event, resolving the platform store;
card bodies, comments and producer SID strings are never credential inputs.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import re
import sqlite3

SOURCE_EVENT = 'native_source_bound'
BOARD = 'registry'


def board_path(home):
    from scripts.kanban_registry_records import checked_path
    root = Path(home).resolve()
    path = checked_path(Path(home) / 'kanban' / 'boards' / BOARD / 'kanban.db', root=root)
    for suffix in ('-wal', '-shm'):
        checked_path(Path(str(path) + suffix), root=root)
    checked_path(path.parent / 'board.json', root=root)
    return path


def source_index(conn):
    """Audit metadata belongs to its native card; uniqueness includes closed cards."""
    from hermes_cli import kanban_db as kb
    if not kb.board_tracking_only(conn):
        raise PermissionError('coordination requires a tracking-only board')
    tasks, keys, cards = {}, set(), set()
    for row in conn.execute('SELECT task_id, payload FROM task_events WHERE kind=? ORDER BY id', (SOURCE_EVENT,)):
        spec = json.loads(row['payload'])
        tid = spec['task_id']
        if tid in tasks or spec['session_key'] in keys or row['task_id'] in cards:
            raise PermissionError('duplicate native task/source/card binding')
        card = kb.get_task(conn, row['task_id'])
        if not card or card.project_id != spec['project_id']:
            raise PermissionError('native task project binding changed')
        # A user pause closes admission, but receipts already in flight may return.
        paused = card.status in {'done', 'archived'}
        if card.status == 'blocked':
            events = [e for e in kb.list_events(conn, card.id) if e.kind in {'blocked', 'tracking_status'}]
            if events:
                last = events[-1]
                paused = (last.payload.get('reason') != 'initial_status' if last.kind == 'blocked' else
                          last.payload.get('actor') not in {'native-task-intake', 'kanban-registry/v1'})
        tasks[tid] = dict(spec, native_id=card.id, project=spec['document_project'],
                          chat_id=spec['origin']['chat_id'], parent_chat_id=spec['origin'].get('parent_chat_id'),
                          admissible=not paused, kind='task')
        keys.add(spec['session_key'])
        cards.add(card.id)
    return tasks


def read_sources(home):
    path = board_path(home)
    if not path.is_file():
        return {}
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        return source_index(conn)


def project_exists(home, project_id, *, allow_archived=False):
    from hermes_cli import projects_db as pdb
    path = Path(home) / 'projects.db'
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        project = pdb.get_project(conn, project_id)
    if not project or project.archived and not allow_archived:
        raise PermissionError('native project missing or archived')
    return project.id


def validate_source(policy, source, key):
    from gateway.config import Platform
    profile = source.profile or 'default'
    if (source.platform != Platform.DISCORD or source.chat_type != 'thread'
            or not source.thread_id or source.thread_id != source.chat_id
            or source.user_id != policy['owner_user_id'] or source.scope_id != policy['guild_id']
            or profile not in policy['mains']
            or source.parent_chat_id not in policy.get('task_parents', {}).get(profile, [])
            or key in {m['session_key'] for m in policy['mains'].values()}):
        raise PermissionError('native task owner/guild/profile/parent refused')


def _subscribe_source(conn, native_id, source):
    from hermes_cli import kanban_db_notify as notify
    profile = source.profile or 'default'
    for sub in notify.list_notify_subs(conn):
        if (sub['task_id'], sub['platform'], sub['chat_id'], sub['thread_id']) == (
                native_id, source.platform.value, source.chat_id, source.thread_id or ''):
            if sub.get('notifier_profile') not in (None, '', profile):
                raise PermissionError('native source subscription belongs to another profile')
    notify.add_notify_sub(conn, task_id=native_id, platform=source.platform.value,
                         chat_id=source.chat_id, thread_id=source.thread_id, user_id=source.user_id,
                         chat_type=source.chat_type, notifier_profile=profile,
                         delivery_metadata={'parent_chat_id': source.parent_chat_id,
                                            'scope_id': source.scope_id})


def accept_task(conn, *, policy, source, session_key, session_id, profile_home,
                task_id, document_project, project_id, title=None, native_id=None, subscribe=True):
    """Compose native card creation and provenance in ONE board writer transaction.

    Called after the local operator resolved an actual persisted native session.
    There is no model-facing handler that accepts a binding or arbitrary SID.
    """
    from hermes_cli import kanban_db as kb
    from scripts.kanban_registry_records import task_key
    validate_source(policy, source, session_key)
    if not project_id:
        raise PermissionError('native project required')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', task_id):
        raise ValueError('stable task id required')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', document_project):
        raise ValueError('canonical document project required')
    profile = source.profile or 'default'
    spec = dict(task_id=task_id, profile=profile, session_key=session_key, session_id=session_id,
                profile_home=str(Path(profile_home).resolve()), origin=source.to_dict(),
                document_project=document_project, project_id=project_id)
    with kb.write_txn(conn):
        index = source_index(conn)
        existing = index.get(task_id)
        if existing:
            from gateway.agent_handoff import _route_anchor
            if (any(existing[k] != v for k, v in spec.items() if k not in {'session_id', 'origin'})
                    or _route_anchor(existing['origin']) != _route_anchor(spec['origin'])
                    or native_id and native_id != existing['native_id']):
                raise PermissionError('native source binding is immutable')
            from gateway.agent_handoff import _continues
            if not _continues(profile_home, existing['session_id'], session_id):
                raise PermissionError('native source session reset')
            if subscribe:
                _subscribe_source(conn, existing['native_id'], source)
            return existing['native_id']
        if any(s['session_key'] == session_key for s in index.values()):
            raise PermissionError('source already belongs to another task')
        key = task_key(dict(profile=profile, task_id=task_id))
        # Historical registration namespace is not the actual runtime profile.
        # It locates an existing card; it grants no endpoint authority.
        matches = [t for t in kb.list_tasks(conn, include_archived=True)
                   if t.idempotency_key == task_key(dict(profile=t.tenant or profile, task_id=task_id))]
        if len(matches) > 1 or native_id and matches and matches[0].id != native_id:
            raise PermissionError('ambiguous native task identity')
        card = kb.get_task(conn, native_id) if native_id else (matches[0] if matches else None)
        if native_id and card is None:
            raise PermissionError('explicit native card missing')
        if card and any(s['native_id'] == card.id for s in index.values()):
            raise PermissionError('native card already bound to a task/source')
        if card and (card.project_id != project_id or card.status in {'done', 'archived'}):
            raise PermissionError('existing card project or closure refused')
        if (card and (card.idempotency_key or '').startswith('kanban-registry/v1:')
                and card.idempotency_key != task_key(dict(profile=card.tenant or profile, task_id=task_id))):
            raise PermissionError('existing stable task id differs')
        created = card is None
        if created:
            native_id = kb.create_task(conn, title=title, board=BOARD, initial_status='blocked',
                                      project_id=project_id, created_by='native-task-intake',
                                      idempotency_key=key, tenant=profile, session_id=session_id, completion_contract='local-only')
        else:
            native_id = card.id
        if subscribe:
            _subscribe_source(conn, native_id, source)
        kb._append_event(conn, native_id, SOURCE_EVENT, spec)
        if created:
            kb.observe_tracking_task(conn, native_id, 'running', actor='native-task-intake',
                                     evidence={'goal_status': 'unverified', 'reason': 'native acceptance'})
        return native_id


def persisted_source(home, key, policy):
    """Read authoritative routing + owning profile's session row; never JSON fallback."""
    from hermes_cli.profiles import get_profile_dir
    from hermes_cli.config_effective import load_user_config_effective
    from gateway.config import GatewayConfig
    from gateway.native_session_identity import persisted_entry, validate_native_session
    home = Path(home).resolve()
    config = load_user_config_effective(home / 'config.yaml', fail_closed=True)
    gateway = GatewayConfig.from_dict(dict(config, sessions_dir=config.get('sessions_dir', str(home / 'sessions'))))
    entry = persisted_entry(home, key, gateway)
    source = entry.origin
    if not source or entry.session_key != key:
        raise PermissionError('native source origin missing')
    validate_source(policy, source, key)
    profile = source.profile or 'default'
    profile_home = home if profile == 'default' else Path(get_profile_dir(profile)).resolve()
    validate_native_session(profile_home, source, key, entry.session_id, entry.session_id)
    return source, entry.session_id, profile_home


def observe_task(conn, native_id, event):
    """Controller observations update this card, never an admission copy or run."""
    from hermes_cli import kanban_db as kb
    from scripts.agent_task_intake import STATES
    from scripts.kanban_registry_records import digest, timestamp, text
    from scripts.kanban_registry_native import desired_status, manual_decision
    kind = event['kind']
    if kind not in STATES or kind == 'accepted':
        raise PermissionError('acceptance uses the native completion/review contract')
    if type(event['revision']) is not int or event['revision'] < 1:
        raise ValueError('positive revision required')
    text(event['summary'], limit=500)
    timestamp(event['observed_at'])
    if type(event['needs_user']) is not bool or kind == 'blocked' and not event['needs_user']:
        raise ValueError('blocked requires an actual user action')
    observation = dict(external_state=STATES[kind], goal_status='unverified',
                       revision=event['revision'], input_digest=digest(event), event=event)
    with kb.write_txn(conn, allow_nested=True):
        spec = next((s for s in source_index(conn).values() if s['native_id'] == native_id), None)
        if not spec or spec['task_id'] != event['task_id']:
            raise PermissionError('native task source missing')
        history = [e.payload for e in kb.list_events(conn, native_id) if e.kind == 'native_task_observed']
        previous = history[-1] if history else None
        if previous and (event['revision'] < previous['revision'] or event['revision'] == previous['revision']
                         and observation['input_digest'] != previous['input_digest']):
            raise PermissionError('stale or conflicting observation')
        if previous and observation['input_digest'] == previous['input_digest']:
            return kb.get_task(conn, native_id)
        card = kb.get_task(conn, native_id)
        preserve = manual_decision(conn, card, previous)
        if not previous and card.created_by == 'native-task-intake':
            preserve = any(e.kind not in {'created', 'native_source_bound'}
                           and not (e.kind == 'blocked' and e.payload.get('reason') == 'initial_status')
                           and not (e.kind == 'tracking_status' and e.payload.get('actor') == 'native-task-intake')
                           for e in kb.list_events(conn, native_id))
        if not preserve:
            if not kb.observe_tracking_task(conn, native_id, desired_status(observation),
                                           actor='native-task-intake', evidence=observation, expected_status=card.status):
                raise PermissionError('native lifecycle transition refused')
        current = kb.get_task(conn, native_id)
        observation.update(applied_native_status=current.status, manual_status_preserved=preserve,
                           native_event_id=conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?',
                                                        (native_id,)).fetchone()[0])
        kb._append_event(conn, native_id, 'native_task_observed', observation)
        return current
