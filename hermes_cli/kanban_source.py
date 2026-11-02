"""Official local operator intake: Kanban is the sole task source."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('accept', 'observe', 'show'))
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--source-key')
    parser.add_argument('--native-id')
    parser.add_argument('--project')
    parser.add_argument('--handoff-path')
    parser.add_argument('--title')
    parser.add_argument('--event', type=Path)
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (PermissionError, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, PermissionError) else 'invalid_or_unavailable_native_source'}))
        return 2


def execute(args):
    from agent.delegation_context import is_delegated_child_process_context, owned_kanban_task
    from gateway.agent_coordination import coordination_policy, document_pointer
    from hermes_constants import get_hermes_home, profile_name_for_home
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_task_source as source_api
    home = Path(get_hermes_home()).resolve()
    if is_delegated_child_process_context() or owned_kanban_task():
        raise PermissionError('worker/delegated intake refused')
    if os.environ.get('HERMES_KANBAN_DB'):
        raise PermissionError('inherited DB pin refused')
    config = load_user_config_effective(home / 'config.yaml', fail_closed=True)
    policy = coordination_policy(SimpleNamespace(home=home, policy=config.get('agent_handoff') or {}))
    if profile_name_for_home(home) not in policy['mains']:
        raise PermissionError('local operator is not an authorized main profile')
    if args.command == 'show':
        spec = policy['tasks'].get(args.task_id)
        if not spec:
            raise PermissionError('native task source missing')
        return spec
    if not args.source_key:
        raise ValueError('source-key required')
    origin, sid, profile_home = source_api.persisted_source(home, args.source_key, policy)
    spec = policy['tasks'].get(args.task_id)
    if args.command == 'accept':
        document = document_pointer(policy, args.task_id, args.handoff_path)
        project_id = source_api.project_exists(home, args.project)
    elif not spec or spec['session_key'] != args.source_key:
        raise PermissionError('task/source mismatch')
    if args.command == 'observe':
        source_api.project_exists(home, spec['project_id'])
        from gateway.agent_handoff import _continues, _same_home, _route_anchor
        if (not _continues(profile_home, spec['session_id'], sid)
                or not _same_home(profile_home, spec['profile_home'])
                or _route_anchor(origin.to_dict()) != _route_anchor(spec['origin'])):
            raise PermissionError('native source reset')
    # Explicit file path: no ambient board, home or DB override can redirect this write.
    path = source_api.board_path(home)
    if not path.is_file():
        raise PermissionError('tracking board must already exist')
    with kbc.connect_closing(db_path=path) as conn:
        if args.command == 'accept':
            native_id = source_api.accept_task(conn, policy=policy, source=origin, session_key=args.source_key,
                session_id=sid, profile_home=profile_home, task_id=args.task_id,
                document_project=document['project'], project_id=project_id, title=args.title, native_id=args.native_id,
                subscribe=(config.get('kanban') or {}).get('auto_subscribe_on_create', True) is True)
        else:
            native_id = spec['native_id']
        if args.event:
            from scripts.kanban_registry_records import read_json
            event = read_json(args.event)
            source_api.observe_task(conn, native_id, event)
        from hermes_cli import kanban_db as kb
        card = kb.get_task(conn, native_id)
        return dict(native_id=native_id, task_id=args.task_id, project_id=card.project_id,
                    status=card.status, source_key=args.source_key, source_session_id=sid,
                    native_runs=len(kb.list_runs(conn, native_id)), task_source='native Kanban card')
