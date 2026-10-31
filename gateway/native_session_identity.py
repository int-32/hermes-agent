"""Read-only native session proof shared by intake, coordination and receipt consumers."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


@contextmanager
def native_state(home):
    from hermes_state import SessionDB
    from scripts.kanban_registry_records import checked_path
    try:
        path = checked_path(Path(home) / 'state.db')
        if not path.is_file():
            raise PermissionError('native session store missing')
        with SessionDB(path, read_only=True) as state:
            yield state
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise PermissionError('native session store unavailable') from exc


def _current_row(state, old, current):
    from hermes_state_common import is_automatic_end_reason
    routed = state.get_session(current) if current else None
    if not old or not routed or state.get_session(old) is None:
        raise PermissionError('native session missing')
    # Compression can commit its child before the routing cache publishes it.
    # Prove that descendant through SessionDB, without inventing/resetting a SID.
    tip = state.get_compression_tip(current)
    row = state.get_session(tip) if tip else routed
    if not row:
        raise PermissionError('native compression tip missing')
    reason = row.get('end_reason')
    if (reason and not is_automatic_end_reason(reason)
            or row.get('ended_at') and not reason
            or row.get('archived') and not row.get('auto_archived')):
        raise PermissionError('native session ended or archived')
    if old != row['id'] and state.get_compression_tip(old) != row['id']:
        raise PermissionError('native session reset or forked')
    return row


def session_continues(home, old, current):
    """Same SID still requires a readable, eligible owning-profile row."""
    try:
        with native_state(home) as state:
            _current_row(state, old, current)
        return True
    except PermissionError:
        return False


def validate_native_session(home, source, key, old, current):
    from gateway.agent_handoff import _route_anchor
    with native_state(home) as state:
        row = _current_row(state, old, current)
        expected = dict(session_key=key, source=source.platform.value,
                        user_id=source.user_id, chat_id=source.chat_id,
                        chat_type=source.chat_type, thread_id=source.thread_id)
        if (any(row.get(k) != value for k, value in expected.items())
                or (row.get('profile_name') or 'default') != (source.profile or 'default')):
            raise PermissionError('native session row ownership changed')
        if row.get('origin_json'):
            origin = json.loads(row['origin_json'])
            if _route_anchor(origin) != _route_anchor(source.to_dict()):
                raise PermissionError('native session row origin changed')
    return row


def persisted_entry(home, key, config):
    from gateway.session import SessionEntry, build_session_key, profile_from_session_key_namespace
    # Exactly the producer's SessionStore._routing_scope; never the empty legacy scope.
    scope = str(config.sessions_dir.resolve())
    with native_state(home) as state:
        raw = state.load_gateway_routing_entries(scope=scope).get(key)
    if not raw:
        raise PermissionError('native source routing missing')
    entry = SessionEntry.from_dict(json.loads(raw))
    if not entry.origin or entry.session_key != key:
        raise PermissionError('native source origin missing')
    parts = key.split(':')
    if len(parts) < 3 or parts[0] != 'agent':
        raise PermissionError('invalid stored native session namespace')
    profile = profile_from_session_key_namespace(parts[1])
    # The persisted key records the producer's resolved mode. Parsing an unset
    # YAML flag cannot reconstruct that boot verdict, and running boot here is unsafe.
    if parts[1] != 'main' and profile != (entry.origin.profile or 'default'):
        raise PermissionError('stored native profile namespace changed')
    expected = build_session_key(entry.origin, group_sessions_per_user=config.group_sessions_per_user,
                                 thread_sessions_per_user=config.thread_sessions_per_user,
                                 profile=None if parts[1] == 'main' else profile)
    if key != expected:
        raise PermissionError('stored source key disagrees with canonical session key')
    return entry
