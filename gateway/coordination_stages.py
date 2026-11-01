"""One server-owned successor of a consumed task-origin receipt, in the same journal.

An intent is neither new permission nor a replay. Only successful native return can
publish it; the existing queued-request consumer revalidates and executes the new scope.
"""
import asyncio
import hashlib
import json
import sqlite3

from gateway.agent_coordination import (
    caller_endpoint, coordination_policy, document_pointer, endpoint, target_endpoint,
)


def return_owner(service):
    """Pin the actual owner before native return, without denying legacy receipt delivery."""
    from gateway.agent_handoff import completion_owner, _HANDOFF_OWNER_LOCK
    with _HANDOFF_OWNER_LOCK:
        try:
            return completion_owner(service)
        except PermissionError:
            # An accepted old return remains eligible, but cannot authorize a new stage.
            return None


def _candidate(work, intent):
    fields = ('protocol', 'kind', 'source_key', 'source_sid', 'caller_home', 'source_endpoint',
              'target_endpoint', 'source_profile', 'target_profile', 'origin', 'target',
              'target_sid', 'target_key', 'lineage', 'task_binding')
    return {**{k: work[k] for k in fields}, 'request_id': intent['request_id'],
            'root_request_id': intent['request_id'], 'stage_after_request_id': work['request_id'],
            'document': intent['document'], 'message': json.dumps(intent['document'], ensure_ascii=False, sort_keys=True),
            'status': 'queued'}


def _eligible(service, work):
    """Use the existing live task/route/session permission roots, including at COMMIT."""
    from gateway.agent_handoff import _route_anchor
    from gateway.native_session_identity import validate_native_session
    from gateway.session import SessionSource
    from hermes_cli.kanban_task_source import project_exists
    if service.policy.get('execution_enabled') is not True:
        raise PermissionError('native execution consent withdrawn')
    policy = coordination_policy(service)
    task = policy['tasks'].get(work['document']['task_id'])
    if not task or not task['admissible']:
        raise PermissionError('native task missing, closed or paused')
    if any(task.get(k) != v for k, v in work['task_binding'].items() if k != 'admissible'):
        raise PermissionError('stage task source changed')
    project_exists(service.home, task['project_id'])
    caller = caller_endpoint(policy, work['source_key'])
    if (caller != work['source_endpoint'] or caller != 'task:' + work['document']['task_id']
            or target_endpoint(policy, caller, 'main', work['document']['task_id']) != work['target_endpoint']):
        raise PermissionError('stage must retain its task and original own-main edge')
    for side in ('source', 'target'):
        spec = endpoint(policy, work[side + '_endpoint'])
        source = SessionSource.from_dict(work['origin' if side == 'source' else 'target'])
        if (spec['session_key'] != work[side + '_key']
                or not service.runner._is_user_authorized_for_source(source, allow_adapter_delegation=False)):
            raise PermissionError('stage native authorization changed')
        home = service.runner._resolve_profile_home_for_source(source)
        validate_native_session(home, source, spec['session_key'], spec['session_id'], work[side + '_sid'])
        if side == 'source' and _route_anchor(source.to_dict()) != _route_anchor(task['origin']):
            raise PermissionError('stage source identity changed')


def _fresh_scope(service, work, document):
    seen = set()
    while work is not None:
        if (work['request_id'] in seen or work['document']['task_id'] != document['task_id']
                or work['document']['project'] != document['project']
                or work['document']['sha256'] == document['sha256']):
            raise PermissionError('next stage requires a new scope, never an ancestor scope replay')
        seen.add(work['request_id'])
        previous = work.get('stage_after_request_id')
        work = service.journal.get(previous) if previous else None
        if previous and work is None:
            raise PermissionError('stage predecessor missing')


async def _durable_call(function, *args):
    writer = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(writer)
    except asyncio.CancelledError:
        await asyncio.gather(writer, return_exceptions=True)
        raise


def _root_return(current):
    if (current.get('parent_request_id')
            or not any(a['phase'] == 'origin' and a['outcome'] == 'completed'
                       for a in current.get('acknowledgements', []))):
        raise PermissionError('stage requires the current completed root return')


def _transaction_record(db, request_id):
    row = db.execute('SELECT record FROM requests WHERE id=?', (request_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def _admit_stage(service, turn):
    from gateway.agent_handoff import completion_owner, _HANDOFF_OWNER_LOCK
    with _HANDOFF_OWNER_LOCK:
        owner = completion_owner(service)
        if turn.stage_owner is not owner:
            raise PermissionError('private receipt belongs to an obsolete stage owner')
        current = service.journal.get(turn.work['request_id'])
        _root_return(current)
        if current['status'] != 'returning':
            raise PermissionError('stage requires the current completed root return')
        return owner


def _write_intent(service, turn, owner, intent):
    from gateway.agent_handoff import completion_owner, _HANDOFF_OWNER_LOCK
    request_id = turn.work['request_id']
    candidate = _candidate(turn.work, intent)
    def guard(db=None):
        if completion_owner(service) is not owner or turn.stage_owner is not owner:
            raise PermissionError('stage runtime owner changed')
        _eligible(service, candidate)
        # Returning only registers an intent; the ordinary publisher checks its scope.
        # A late writer publishes here, so it must perform that check at this COMMIT.
        if db is not None and _transaction_record(db, request_id).get('status') == 'delivered':
            if document_pointer(coordination_policy(service), intent['document']['task_id'],
                                intent['document']['path']) != intent['document']:
                raise PermissionError('stage document changed before publication')
    # Do not hold the owner mutex while waiting to ENTER update_many. A timed-out
    # bridge's already-admitted writer may arrive after the native return commits.
    def prepare(db):
        current = _transaction_record(db, request_id)
        _root_return(current)
        if current.get('next_stage') not in (None, intent):
            raise PermissionError('receipt already has a different next scope')
        if current['status'] == 'returning':
            successor = None
        elif (current['status'] == 'delivered' and current.get('continuation_confirmed') is True
              and current.get('continuation_outcome') == current.get('outcome') == 'completed'):
            # The SAME admitted writer, not a replay of a terminal receipt. Persist
            # both rows together so restart's ordinary queued consumer can find it.
            successor = candidate
        else:
            raise PermissionError('stage requires the current completed root return')
        return {request_id: {'next_stage': intent}}, successor
    try:
        service.journal.update_many({request_id: {'next_stage': intent}}, guard=guard, prepare=prepare)
    except (OSError, sqlite3.Error):
        # Lost COMMIT reply: inspect both rows before treating the write as failed.
        with _HANDOFF_OWNER_LOCK:
            guard()
            saved = service.journal.get(request_id)
            successor = service.journal.get(intent['request_id'])
            if (saved.get('next_stage') != intent or saved['status'] not in {'returning', 'delivered'}
                    or (saved['status'] == 'delivered' and (not successor
                        or any(successor.get(k) != v for k, v in candidate.items() if k != 'status')))):
                raise


async def next_stage(service, key, sid, home, request_id, path):
    from gateway.native_job import PrivateJob, current_job
    async with service.coordination_lock:
        job = current_job()
        turn = job.document_turn if isinstance(job, PrivateJob) else None
        if (turn is None or turn.key != key or turn.work['request_id'] != request_id
                or turn.phase != 'origin' or turn.completion != 'completed'
                or turn.work.get('parent_request_id') or not turn.work.get('task_binding') or not turn.child
                or turn.child.get('outcome') != 'completed'):
            raise PermissionError('next_stage requires a consumed completed private task-origin root receipt')
        owner = await asyncio.to_thread(_admit_stage, service, turn)
        work = turn.work
        policy = await asyncio.to_thread(coordination_policy, service)
        document = await asyncio.to_thread(document_pointer, policy, work['document']['task_id'], path)
        if document['path'] != work['document']['path']:
            raise PermissionError('stage cannot change its task document namespace')
        await asyncio.to_thread(_fresh_scope, service, work, document)
        identity = json.dumps([request_id, document], sort_keys=True, separators=(',', ':'))
        intent = {'request_id': 'stage-' + hashlib.sha256(identity.encode()).hexdigest()[:40], 'document': document}
        candidate = _candidate(work, intent)
        await service.pinned_routes(candidate)
        await _durable_call(_write_intent, service, turn, owner, intent)
        return {'status': 'recorded', 'request_id': intent['request_id'], 'stage_after_request_id': request_id,
                'note': 'Intent only; successful native return is required before queueing. Stage completion is not task completion.'}


def _publish(service, turn, changes, candidate, validation_error):
    from gateway.agent_handoff import completion_owner, _HANDOFF_OWNER_LOCK
    request_id = turn.work['request_id']
    def owner_guard(db=None):
        if completion_owner(service) is not turn.stage_owner:
            raise PermissionError('stage runtime owner changed before publication')
    def stage_guard(db=None):
        owner_guard()
        _eligible(service, candidate)
    with _HANDOFF_OWNER_LOCK:
        owner_guard()
        # Reconcile an already committed transaction; never turn a lost reply into a new stage.
        current = service.journal.get(request_id)
        if current['status'] == 'delivered':
            return
        if current['status'] != 'returning' or current.get('next_stage', {}).get('request_id') != candidate['request_id']:
            raise PermissionError('stage return association changed')
        try:
            if validation_error:
                raise PermissionError(validation_error)
            service.journal.update_many(changes, successor=candidate, guard=stage_guard)
        except PermissionError as error:
            # A task may close after the accepted dependency executed. Keep its legitimate
            # return while denying the new stage; an obsolete runtime cannot publish either.
            owner_guard()
            rejected = {k: dict(v) for k, v in changes.items()}
            rejected[request_id]['next_stage_error'] = str(error)
            service.journal.update_many(rejected, guard=owner_guard)
        except (OSError, sqlite3.Error):
            saved = service.journal.get(request_id)
            successor = service.journal.get(candidate['request_id'])
            if (saved['status'] != 'delivered' or not successor
                    or any(successor.get(k) != v for k, v in candidate.items() if k != 'status')):
                raise


def _settle_without_intent(service, request_id, changes):
    from gateway.agent_handoff import _HANDOFF_OWNER_LOCK
    def prepare(db):
        current = _transaction_record(db, request_id)
        if current.get('next_stage') and changes[request_id]['status'] == 'delivered':
            return {}, None  # Writer won; caller must validate and publish that intent.
        return changes, None
    try:
        return service.journal.update_many(changes, prepare=prepare)
    except (OSError, sqlite3.Error):
        with _HANDOFF_OWNER_LOCK:
            saved = {key: service.journal.get(key) for key in changes}
            if all(row and all(row.get(k) == v for k, v in changes[key].items())
                   for key, row in saved.items()):
                return saved
            current = saved[request_id]
            if (current['status'] == 'returning' and current.get('next_stage')
                    and changes[request_id]['status'] == 'delivered'):
                return {}  # The intent won, not an uncommitted successful return.
            raise


async def settle_return(service, record, turn, changes):
    request_id = record['request_id']
    while True:
        current = await asyncio.to_thread(service.journal.get, request_id)
        intent = current.get('next_stage')
        if not intent or changes[request_id]['status'] != 'delivered':
            if await _durable_call(_settle_without_intent, service, request_id, changes):
                return
            continue
        candidate = _candidate(current, intent)
        error = None
        try:
            await service.pinned_routes(candidate)
        except (PermissionError, OSError, ValueError) as exc:
            error = str(exc)
        await _durable_call(_publish, service, turn, changes, candidate, error)
        return
