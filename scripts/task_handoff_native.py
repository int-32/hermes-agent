"""Read-only native coordination collector. Never constructs a service/store or a turn.

The live bridge is deliberately process-local. Serialized caller claims and HWB
records are not evidence inputs. Standalone collection fails closed without it.
"""
from __future__ import annotations

from contextlib import contextmanager
import asyncio
import json
import math
from pathlib import Path
import sqlite3
import time
import sys
from types import SimpleNamespace

HEADER = '[Native document handoff; attribution and routing below are gateway-owned.]'


def runtime_bridge():
    module = sys.modules.get('gateway.agent_handoff')
    if module is None or getattr(module, '_service', None) is None:
        return None
    try:
        from gateway.agent_handoff import _service
        from gateway.agent_coordination import coordination_policy, native_endpoint, caller_endpoint, target_endpoint
    except ImportError:
        return None
    if _service is None or not _service.runner._running:
        return None
    return SimpleNamespace(service=_service, policy=coordination_policy(_service), native_endpoint=native_endpoint,
                           caller_endpoint=caller_endpoint, target_endpoint=target_endpoint)


@contextmanager
def readonly(path):
    path = Path(path)
    if not path.is_file() or path.resolve(strict=True) != path:
        raise OSError('missing or noncanonical native store')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def journal_record(home, request_id):
    """Do not instantiate HandoffJournal: its constructor performs recovery writes."""
    path = home / 'runtime' / 'agent_handoff.db'
    with readonly(path) as db:
        objects = db.execute("SELECT type FROM sqlite_schema WHERE name='requests'").fetchall()
        if objects != [('table',)]:
            raise ValueError('requests must be a native table, never a view')
        columns = db.execute('PRAGMA table_info(requests)').fetchall()
        if [(r[1], r[2], r[5]) for r in columns] != [('id', 'TEXT', 1), ('record', 'TEXT', 0)]:
            raise ValueError('unsupported native journal schema')
        row = db.execute('SELECT record FROM requests WHERE id=?', (request_id,)).fetchone()
    record = json.loads(row[0]) if row else None
    if record is not None and not isinstance(record, dict):
        raise ValueError('malformed receipt')
    return record


def continuation(home, old, current):
    """Use the same read-only row/lifecycle/lineage proof as native consumers."""
    from gateway.native_session_identity import session_continues
    return session_continues(home, old, current)


def route_pairs(source, task, is_task):
    return [(source.get('platform'), 'discord'), (source.get('profile') or 'default', task.profile),
            (source.get('user_id'), task.user), (source.get('scope_id'), task.guild),
            (source.get('chat_id'), task.thread if is_task else task.main_channel),
            (source.get('thread_id') or '', task.thread if is_task else ''),
            (source.get('parent_chat_id') or '', task.main_channel if is_task else ''),
            (source.get('chat_type'), 'thread' if is_task else 'group')]


async def endpoints(task, bridge, report):
    stage, policy = 'handoff_received', bridge.policy
    service = bridge.service
    task_name, main_name = 'task:' + task.task_id, 'main:' + task.profile
    spec = (policy.get('tasks') or {}).get(task.task_id)
    main = (policy.get('mains') or {}).get(task.profile)
    if not spec or not main:
        report.add(stage, 'native_card_source', 'UNVERIFIED', 'live:coordination_policy/endpoints',
                   'native card source/main binding missing; no identity was created')
        return None
    valid = report.expect(stage, 'native_task_policy', [(spec.get('project'), task.project),
                          (spec.get('profile'), task.profile), (spec.get('chat_id'), task.thread),
                          (spec.get('parent_chat_id'), task.main_channel), (main.get('chat_id'), task.main_channel)],
                          'live:coordination_policy/tasks/' + task.task_id)
    sources, sids = {}, {}
    for name, is_task in ((main_name, False), (task_name, True)):
        try:
            source, sid = await bridge.native_endpoint(service, policy, name)
        except PermissionError:
            report.add(stage, 'native_endpoint:' + name, 'FAIL', 'live:native_endpoint',
                       'native owner, canonical route, authorization or SID continuity rejected')
            return None
        home = await asyncio.to_thread(service.runner._resolve_profile_home_for_source, source)
        if await asyncio.to_thread(Path(home).resolve) != task.home:
            report.add(stage, 'endpoint_home', 'FAIL', 'live:profile home resolver', 'native endpoint belongs to a different profile home')
            return None
        sources[name], sids[name] = source.to_dict(), sid
        valid = report.expect(stage, 'native_endpoint:' + name, route_pairs(sources[name], task, is_task),
                              'live:native_endpoint/' + name) and valid
    if spec.get('session_key') == main.get('session_key') or not spec.get('session_key') or not main.get('session_key'):
        report.add(stage, 'distinct_native_keys', 'FAIL', 'live:coordination_policy', 'task and main must have distinct keys')
        return None
    # Native receipt revalidation checks the current edge as well as both endpoints.
    # Use only its pure guards: pinned_document_routes can update the journal's SID.
    try:
        caller = bridge.caller_endpoint(policy, main['session_key'])
        target = bridge.target_endpoint(policy, caller, task_name, task.task_id)
    except PermissionError:
        report.add(stage, 'native_current_edge', 'FAIL', 'live:caller_endpoint/target_endpoint',
                   'native source registration is ambiguous or current edge is not allowed')
        return None
    if not report.expect(stage, 'native_current_edge', [(caller, main_name), (target, task_name)],
                         'live:caller_endpoint/target_endpoint'):
        return None
    return (spec, main, sources, sids) if valid else None


def pinned_record(task, pointer, record, resolved, report, journal_home):
    spec, main, _sources, sids = resolved
    stage, source = 'handoff_received', str(journal_home / 'runtime' / 'agent_handoff.db') + '#requests/' + task.request_id
    pairs = [(record.get('request_id'), task.request_id), (record.get('kind'), 'coordination'),
             (record.get('protocol'), 2), (record.get('document'), pointer),
             (record.get('source_endpoint'), 'main:' + task.profile),
             (record.get('target_endpoint'), 'task:' + task.task_id),
             (record.get('source_profile'), task.profile), (record.get('target_profile'), task.profile),
             (record.get('source_key'), main['session_key']), (record.get('target_key'), spec['session_key']),
             (record.get('caller_home'), str(task.home))]
    pairs += route_pairs(record.get('origin') or {}, task, False) + route_pairs(record.get('target') or {}, task, True)
    valid = report.expect(stage, 'pinned_handoff', pairs, source)
    for prefix, name in (('source', 'main:' + task.profile), ('target', 'task:' + task.task_id)):
        same = continuation(task.home, record.get(prefix + '_sid'), sids[name])
        valid = report.expect(stage, prefix + '_sid_lineage', [(same, True)], source + '#' + prefix + '_sid') and valid
    now = time.time()
    timestamps = [record.get(k) for k in ('created_at', 'updated_at', 'request_admitted_at')]
    sane = all(type(t) in (int, float) and math.isfinite(t) and 0 < t <= now + 5 for t in timestamps)
    fresh = sane and timestamps[0] <= timestamps[2] <= timestamps[1] and now - timestamps[1] <= task.max_age
    if not fresh:
        report.add(stage, 'native_admission', 'UNVERIFIED', source,
                   'missing, inconsistent, future or stale native admission/receipt timestamp')
        valid = False
    else:
        report.add(stage, 'native_admission', 'PASS', source + '#request_admitted_at', 'gateway-owned admission timestamp present and fresh')
    if record.get('status') not in {'running', 'generated', 'waiting_children', 'returning', 'delivered', 'blocked'} or record.get('error'):
        report.add(stage, 'receipt_state', 'UNVERIFIED', source + '#status', 'unknown, queued, failed or uncertain native receipt cannot authorize execution')
        valid = False
    return valid


def transcript(task, record, current_sid):
    """Read only this request's bounded task-session interval, never controller chat history."""
    from hermes_state import SessionDB
    db = SessionDB(task.home / 'state.db', read_only=True)
    try:
        chain = db.get_compression_chain(record['target_sid'])
    finally:
        db.close()
    if not chain or chain[-1] != current_sid or len(chain) > 100:
        raise ValueError('unsupported or changed compression lineage')
    rows = []
    with readonly(task.home / 'state.db') as db:
        for sid in chain:
            # User bodies carry the gateway-owned envelope. Tool/result bodies are never loaded.
            found = db.execute("SELECT id, role, CASE WHEN role='user' THEN content ELSE NULL END, "
                               "display_metadata, tool_calls, tool_call_id, tool_name FROM messages "
                               "WHERE session_id=? AND timestamp>=? ORDER BY id LIMIT 2001",
                               (sid, record['created_at'])).fetchall()
            rows.extend(found)
            if len(rows) > 2000:
                raise ValueError('request evidence exceeds bounded transcript read')
    return rows


def matching_marker(row, task, record):
    _, role, content, metadata, _, _, _ = row
    if role != 'user' or not content or not content.startswith(HEADER + '\n'):
        return False
    author = json.loads(metadata or '{}').get('native_handoff_author') or {}
    if author.get('id') != 'bot:' + task.profile or author.get('is_bot') is not True:
        return False
    envelope = json.loads(content.split('\n', 2)[1])
    expected = dict(record['document'], request_id=task.request_id,
                    target_endpoint=record['target_endpoint'], return_session=record['source_key'])
    return all(envelope.get(key) == value for key, value in expected.items())


def execution_evidence(task, record, rows, report):
    active, received, executed, calls = False, False, False, set()
    for row in rows:
        _, role, _, _, tool_calls, call_id, tool_name = row
        if role == 'user':
            active = matching_marker(row, task, record)
            received = received or active
            calls = set()
        elif active and role == 'assistant':
            for call in json.loads(tool_calls or '[]'):
                if call.get('id') and (call.get('function') or {}).get('name'):
                    calls.add((call['id'], call['function']['name']))
        elif active and role == 'tool' and (call_id, tool_name) in calls:
            executed = True
    source = str(task.home / 'state.db') + '#messages/task-session+compression-lineage/request=' + task.request_id
    report.add('handoff_received', 'trusted_task_turn', 'PASS' if received else 'UNVERIFIED', source,
               'gateway-authored task envelope matched' if received else 'no trusted correlated task turn; a post/queue/worker is insufficient')
    report.add('execution_started', 'task_tool_activity', 'PASS' if executed else 'UNVERIFIED', source,
               'matched assistant tool call and native tool result inside correlated task turn' if executed
               else 'no correlated task-session tool call/result pair')


def result_evidence(task, record, report, read_discord, journal_home):
    ids, response = record.get('target_message_ids'), record.get('response')
    source = str(journal_home / 'runtime' / 'agent_handoff.db') + '#target_message_ids+response'
    if (record.get('status') not in {'generated', 'waiting_children', 'delivered', 'returning', 'blocked'}
            or not isinstance(ids, list) or not ids or len(ids) > 20 or not isinstance(response, str) or not response.strip()):
        report.add('result_routed', 'trusted_result', 'UNVERIFIED', source, 'no successful native task generation/delivery receipt')
        return
    contents = []
    for message_id in ids:
        if not isinstance(message_id, str) or not message_id.isdecimal():
            report.add('result_routed', 'result_message_id', 'FAIL', source, 'invalid pinned message id')
            return
        path = f'/channels/{task.thread}/messages/{message_id}'
        message = read_discord(task, report, 'result_routed', 'result_readback', path)
        if message is not None:
            report.expect('result_routed', 'result_destination', [(message.get('id'), message_id),
                          (message.get('channel_id'), task.thread)], 'discord:GET ' + path)
            contents.append(message.get('content'))
    if len(contents) == len(ids):
        report.expect('result_routed', 'result_body', [(''.join(contents) if all(isinstance(c, str) for c in contents) else None,
                      response)], source + '+discord:GET pinned task message(s)')


async def collect(task, pointer, report, read_discord):
    """Only direct native process objects authorize native stages. No deserialized proof mode."""
    try:
        bridge = runtime_bridge()
        if bridge is None:
            report.add('handoff_received', 'live_native_context', 'UNVERIFIED', 'process:gateway.agent_handoff._service',
                       'no supported live native service in this process; do not construct it or bind a caller')
            return
        resolved = await endpoints(task, bridge, report)
        if resolved is None or pointer is None:
            return
        if not task.request_id:
            report.add('handoff_received', 'request_id', 'UNVERIFIED', 'input:request-id', 'no explicit native request selected')
            return
        journal_home = Path(bridge.service.home)
        record = await asyncio.to_thread(journal_record, journal_home, task.request_id)
        if record is None:
            report.add('handoff_received', 'native_receipt', 'UNVERIFIED', str(journal_home / 'runtime' / 'agent_handoff.db'),
                       'native request not found')
            return
        if not await asyncio.to_thread(pinned_record, task, pointer, record, resolved, report, journal_home):
            return
        rows = await asyncio.to_thread(transcript, task, record, resolved[3]['task:' + task.task_id])
        execution_evidence(task, record, rows, report)
        await asyncio.to_thread(result_evidence, task, record, report, read_discord, journal_home)
    except Exception as exc:  # health: allow BLE001 -- collector must redact native data errors into nonzero UNVERIFIED
        report.add('handoff_received', 'native_collector', 'UNVERIFIED', 'native:direct read-only APIs/stores',
                   'native evidence unavailable: ' + type(exc).__name__)
    finally:
        # Every stage must have affirmative evidence; missing earlier work cannot vacuously PASS.
        for stage in ('handoff_received', 'execution_started', 'result_routed'):
            if not any(row['stage'] == stage for row in report.checks):
                report.add(stage, 'evidence_missing', 'UNVERIFIED', 'native:collector', 'required native stage has no evidence')
