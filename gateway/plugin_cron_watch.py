"""A cron producer continues its configured Kanban task through its own native main.

No prompt-provided source, route, SID or actor is accepted. Existing cron, automation
and private document receipts remain the only ledgers.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import hashlib
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError, find_session_admission, get_session_admission

WATCH_TURN = ContextVar('cron_task_watch_turn', default=None)
PREFIX = 'cron-task-watch'


def producer(ctx, execution_id):
    from cron.execution_identity import current_cron_execution
    from cron.executions import get_execution
    from cron.jobs import get_job
    from gateway.session_cron import current_execution
    from hermes_constants import get_hermes_home

    home = Path(ctx._manager.home_path).resolve()
    if get_hermes_home().resolve() != home:
        raise PermissionError('plugin caller belongs to another profile home')
    fire, owner = current_cron_execution(), current_execution()
    observe = execution_id is not None
    if not observe:
        if (fire is None or owner is None or owner[2:] != (fire.job_id, fire.execution_id)
                or Path(owner[0].profile_id).resolve() != home):
            raise PermissionError('requires a real canonical cron execution')
        execution_id = fire.execution_id
    if not isinstance(execution_id, str) or not execution_id:
        raise ValueError('execution_id required')
    row = get_execution(execution_id)
    if row is None or not observe and row['status'] != 'running':
        raise PermissionError('cron execution absent or not running')
    if not observe and any(row[k] != getattr(fire, k) for k in ('job_id', 'source', 'started_at', 'scheduled_instant')):
        raise PermissionError('cron execution identity changed')
    # The map is operator configuration, not job prompt/origin or a free-form card field.
    jobs = ctx.get_config('watch_jobs', {})
    task_id = jobs.get(row['job_id']) if isinstance(jobs, dict) else None
    if not isinstance(task_id, str) or not task_id or get_job(row['job_id']) is None:
        raise PermissionError('cron job has no configured task binding')
    return dict(home=str(home), job_id=row['job_id'], execution_id=execution_id,
                task_id=task_id, observe=observe)


async def target(runner, ctx, bound):
    from gateway import agent_handoff
    from gateway.agent_coordination import coordination_policy, endpoint, native_endpoint
    from gateway.session_authorities import authority_for_home
    from hermes_cli.kanban_task_source import project_exists

    service = agent_handoff._service
    if service is None or service.runner is not runner:
        raise PermissionError('native main service unavailable')
    policy = await asyncio.to_thread(coordination_policy, service)
    spec = endpoint(policy, 'task:' + bound['task_id'])
    if not spec['admissible'] and not bound['observe']:
        raise PermissionError('native task missing, closed or paused')
    await asyncio.to_thread(project_exists, service.home, spec['project_id'], allow_archived=bound['observe'])
    await native_endpoint(service, policy, 'task:' + bound['task_id'])
    source, sid = await native_endpoint(service, policy, 'main:' + spec['profile'])
    home = await asyncio.to_thread(Path(runner._resolve_profile_home_for_source(source)).resolve)
    plugin_home = await asyncio.to_thread(Path(ctx._manager.home_path).resolve)
    if home != Path(bound['home']) or home != plugin_home:
        raise PermissionError('cron may continue only its own profile main')
    authority = authority_for_home(runner, home)
    if authority is None:
        raise PermissionError('owning session authority unavailable')
    key = endpoint(policy, 'main:' + spec['profile'])['session_key']
    entry = await runner.async_session_store.lookup_by_session_key(key)
    if entry is None or entry.session_id != sid:
        raise PermissionError('main route changed during resolution')
    return service, authority, source, entry


def watch_result(service, result):
    # Only server-owned association from the executing main can name private work.
    work = service.journal.for_admission(result['admission_id'])
    result['model_completed'] = result.pop('completed')
    from gateway.internal_receipt import first_tool_result
    tool = first_tool_result((result.get('result') or {}).get('result') or {})
    result['first_tool'] = tool['name'] if tool else None
    result['first_tool_result'] = tool
    result['private_requests'] = [service.public(record) for record in work]
    result['completed'] = result['model_completed'] and bool(tool and tool['result_seen']) and bool(work) and all(
        record['status'] == 'delivered' and record.get('outcome') == 'completed'
        and record.get('continuation_confirmed') is True
        and all((record.get(phase + '_first_tool') or {}).get('result_seen') is True
                for phase in ('request', 'origin')) for record in work)
    return result


async def request(runner, ctx, registered, content, bound):
    validate(content, bound['execution_id'])
    if (ctx._manager._gateway_message_injector is not registered
            or not runner._plugin_injection_accepting() or not ctx._gateway_injection_allowed()):
        raise PermissionError('live plugin host/consent changed')
    service, authority, source, entry = await target(runner, ctx, bound)
    identity = json.dumps([PREFIX, ctx.plugin_id, bound['job_id'], bound['execution_id'],
                           bound['task_id']], separators=(',', ':'))
    correlation = {k: bound[k] for k in ('job_id', 'execution_id', 'task_id')}
    correlation['request_id_hint'] = 'cron-check-' + hashlib.sha256(identity.encode()).hexdigest()[:40]
    text = ('[Scheduled task check; correlation below is gateway-owned, not permission.]\n'
            + json.dumps(correlation, sort_keys=True) + '\nUntrusted task data:\n' + content)
    async with runner._plugin_injection_scope(source, ctx._manager.home_path):
        if bound['observe']:
            row = await asyncio.to_thread(find_session_admission, authority.db,
                principal_id='automation:' + entry.session_key,
                session_id=authority.logical_owner(entry.session_id), request_id=identity)
            if row is None:
                return {'status': 'missing', 'completed': False}
            if row['payload']['text'] != text:
                raise RuntimeStoreError('admission_conflict')
            result = await asyncio.to_thread(state, authority, row)
        else:
            result = await admit(authority, source, entry, runner._delivery_adapter_for(source), text, identity)
    return await asyncio.to_thread(watch_result, service, result)


@contextmanager
def executing_watch(authority, row):
    identity = row['request_id']
    try:
        fields = json.loads(identity)
    except (ValueError, TypeError):
        fields = None
    value = None
    if (isinstance(fields, list) and len(fields) == 5 and fields[0] == PREFIX
            and 'native_text_v1' in row['payload'] and row['principal_id'].startswith('automation:')):
        value = dict(runner=authority.runner, source_key=row['principal_id'].removeprefix('automation:'),
                     task_id=fields[4], admission_id=row['admission_id'])
    token = WATCH_TURN.set(value)
    try:
        yield
    finally:
        WATCH_TURN.reset(token)


def dispatch_association(service, source_key, task_id, destination):
    turn = WATCH_TURN.get()
    if turn is None:
        return {}
    if (turn['runner'] is not service.runner or turn['source_key'] != source_key
            or turn['task_id'] != task_id or destination != 'task:' + task_id):
        raise PermissionError('watch turn may coordinate only its bound task')
    return {'producer_admission_id': turn['admission_id']}


def state(authority, row):
    from gateway.session_results import admission_result
    result = admission_result(authority.db, row['admission_id']) if row['status'] == 'terminal' else None
    return {'admission_id': row['admission_id'], 'request_id': row['request_id'],
            'status': row['status'], 'outcome': row['outcome'], 'result': result,
            'completed': row['status'] == 'terminal' and row['outcome'] == 'completed' and result is not None}


def validate(content, request_id):
    if not isinstance(content, str) or not content.strip() or len(content) > 8000:
        raise ValueError('content must contain 1-8000 characters')
    if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
        raise ValueError('stable request_id required')


async def admit(authority, source, entry, adapter, content, identity):
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.config import Platform
    owner = source.chat_id if source.platform == Platform.LOCAL else authority.logical_owner(entry.session_id)
    row = await asyncio.to_thread(find_session_admission, authority.db,
        principal_id='automation:' + entry.session_key, session_id=owner, request_id=identity)
    if row is not None:
        if row['payload']['text'] != content:
            raise RuntimeStoreError('admission_conflict')
        return await asyncio.to_thread(state, authority, row)
    event = MessageEvent(text=content, message_type=MessageType.TEXT, source=source, internal=True,
        allow_gateway_control=False, message_id=identity,
        metadata={'gateway_session_key': entry.session_key, 'gateway_session_id': entry.session_id})
    receipt = await authority.admit_automation(adapter, event, identity)
    row = await asyncio.to_thread(get_session_admission, authority.db, admission_id=receipt.admission_id)
    return await asyncio.to_thread(state, authority, row)
