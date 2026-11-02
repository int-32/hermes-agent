"""A receiving main initializes a separate work source for its existing private dependency.

No user input, SID, route or authority is accepted from the caller. The business
source/document and journal parent/root stay intact; only the execution lane differs.
"""
import asyncio
import re
import weakref


def work_reference(binding):
    return {k: binding[k] for k in ('task_id', 'native_id', 'profile', 'session_key')}


def receiving_main(service, policy, key):
    from gateway.agent_coordination import caller_endpoint
    from gateway.native_job import current_document_turn
    turn = current_document_turn()
    caller = caller_endpoint(policy, key)
    if (turn is None or turn.key != key or turn.phase != 'request' or turn.completion
            or caller != turn.work['target_endpoint'] or not caller.startswith('main:')
            or not turn.work['source_endpoint'].startswith('main:')
            or turn.work['source_profile'] == turn.work['target_profile']):
        raise PermissionError('work initialization requires the private receiving peer-main turn')
    current = service.journal.get(turn.work['request_id'])
    if (not current or current['status'] not in {'running', 'waiting_children'}
            or current['target_key'] != key or current['document'] != turn.work['document']
            or current['root_request_id'] != turn.work['root_request_id']):
        raise PermissionError('work initialization lost its active dependency parent')
    return caller, turn


async def thread_source(service, policy, caller, thread_id):
    from gateway.agent_coordination import native_endpoint
    from gateway.session import SessionSource
    from gateway.config import Platform
    from hermes_cli.kanban_task_source import validate_source
    main, _ = await native_endpoint(service, policy, caller)
    adapter = service.runner._delivery_adapter_for(main)
    # This is the adapter's existing cached/REST resolver. Only trusted channel
    # metadata, never a caller-supplied parent/profile, defines the new route.
    channel = await adapter._resolve_channel(thread_id)
    if (channel is None or channel.type.name not in {'public_thread', 'private_thread', 'news_thread'}
            or str(channel.id) != thread_id or str(channel.guild.id) != policy['guild_id']
            or channel.archived or channel.locked):
        raise PermissionError('work destination must be a live native guild thread')
    source = SessionSource(Platform.DISCORD, thread_id, chat_type='thread', thread_id=thread_id,
                           parent_chat_id=str(channel.parent_id), scope_id=str(channel.guild.id),
                           user_id=policy['owner_user_id'])
    source._transport_adapter_ref = weakref.ref(adapter)
    identity = service.runner._canonicalize(source)
    if identity is None or identity.runtime_profile != caller.partition(':')[2]:
        raise PermissionError('work thread does not canonically route to its owning main profile')
    key = service.runner._session_key_for_source(source)
    validate_source(policy, source, key)
    if not service.runner._is_user_authorized_for_source(source, allow_adapter_delegation=False):
        raise PermissionError('native work owner authorization refused')
    return source, key


def initialization_owner(service, owner):
    from gateway.agent_handoff import completion_owner
    if completion_owner(service) is not owner:
        raise PermissionError('work initialization runtime owner changed')


def _reserve_work(service, policy, caller, turn, source, key, work_task_id, owner):
    from gateway.agent_handoff import _HANDOFF_OWNER_LOCK
    from gateway.native_session_identity import validate_native_session
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_task_source import accept_task, board_path
    runner = service.runner
    home = runner._resolve_profile_home_for_source(source)
    with _HANDOFF_OWNER_LOCK:
        initialization_owner(service, owner)
        receiving_main(service, policy, turn.key)
        # An unrelated existing route is not adopted; a failed partial allocation
        # remains inert and requires explicit inspection rather than a new SID.
        if runner.session_store.lookup_by_session_key(key) is not None:
            raise PermissionError('unbound existing work session cannot be adopted')
        with runner._profile_scope_for_source(source):
            entry = runner.session_store.get_or_create_session(source, touch_activity=False)
        validate_native_session(home, source, key, entry.session_id, entry.session_id)
        business = policy['tasks'][turn.work['document']['task_id']]
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(service.home, hydrate_secrets=False), kbc.connect_closing(db_path=board_path(service.home)) as conn:
            native_id = accept_task(conn, policy=policy, source=source, session_key=key,
                session_id=entry.session_id, profile_home=home, task_id=work_task_id,
                document_project=turn.work['document']['project'], project_id=business['project_id'],
                title=work_task_id, work_for=work_reference(business), assignee=source.profile)
        return native_id


async def initialize_task(service, key, sid, home, request_id, work_task_id, thread_id, owner):
    from gateway.agent_coordination import coordination_policy, document_pointer, native_endpoint, submit_document
    from hermes_cli.kanban_task_source import project_exists
    if (not isinstance(work_task_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', work_task_id) is None
            or not isinstance(thread_id, str) or re.fullmatch(r'[1-9][0-9]{0,23}', thread_id) is None):
        raise ValueError('stable work_task_id and native thread_id required')
    from gateway.agent_handoff import validate_request, _HANDOFF_OWNER_LOCK
    validate_request(request_id, 'internal task initialization')
    with _HANDOFF_OWNER_LOCK:
        initialization_owner(service, owner)
    async with service.coordination_lock:
        policy = coordination_policy(service)
        caller, turn = receiving_main(service, policy, key)
        document = turn.work['document']
        business = policy['tasks'].get(document['task_id'])
        if (not business or not business['admissible'] or work_task_id == document['task_id']
                or service.policy.get('execution_enabled') is not True):
            raise PermissionError('independent work requires an active business source and execution consent')
        project_exists(service.home, business['project_id'])
        if await asyncio.to_thread(document_pointer, policy, document['task_id'], document['path']) != document:
            raise PermissionError('business document version changed before work initialization')
        await native_endpoint(service, policy, caller)
        source, target_key = await thread_source(service, policy, caller, thread_id)
        from gateway.native_job import current_lineage
        lineage = current_lineage() or (key,)
        if lineage[-1] != key or target_key in lineage or len(lineage) >= 4:
            raise PermissionError('recursive work initialization loop refused')
        existing = policy['tasks'].get(work_task_id)
        if existing:
            if (existing['session_key'] != target_key or existing.get('work_for') != work_reference(business)
                    or existing['profile'] != caller.partition(':')[2] or not existing['admissible']):
                raise PermissionError('work source already belongs to a different task, route or closed goal')
            await native_endpoint(service, policy, 'task:' + work_task_id)
            native_id = existing['native_id']
        else:
            if any(t['session_key'] == target_key for t in policy['tasks'].values()):
                raise PermissionError('work thread already belongs to another task source')
            native_id = await asyncio.to_thread(_reserve_work, service, policy, caller, turn, source,
                                                target_key, work_task_id, owner)
    # The ordinary writer binds parent/root to the actual receiving turn. Its
    # existing same-business-document guard is retained, not bypassed by a new root.
    result = await submit_document(service, key, sid, home, request_id, document['task_id'],
                                    document['path'], 'task:' + work_task_id, work_task_id=work_task_id, work_owner=owner)
    return dict(result, initialized=True, work_task_id=work_task_id, native_id=native_id)


def dispatch_binding(service, policy, caller_key, task_id, path, work_task_id):
    if work_task_id is None:
        return {}
    caller, turn = receiving_main(service, policy, caller_key)
    binding = policy['tasks'].get(work_task_id)
    business = policy['tasks'][task_id]
    if (turn.work['document']['task_id'] != task_id or turn.work['document']['path'] != path
            or not binding or binding.get('work_for') != work_reference(business)
            or binding['profile'] != caller.partition(':')[2] or not binding['admissible']):
        raise PermissionError('work dispatch must retain its trusted business association')
    return {'work_task_binding': binding}


def validate_record(service, policy, record):
    pinned = record.get('work_task_binding')
    if pinned is None:
        return
    binding = policy['tasks'].get(pinned['task_id'])
    business = policy['tasks'].get(record['document']['task_id'])
    if (not binding or not business or binding.get('work_for') != work_reference(business)
            or any(binding.get(k) != v for k,v in pinned.items() if k != 'admissible')
            or record['target_endpoint'] != 'task:' + binding['task_id']
            or record['source_endpoint'] != 'main:' + binding['profile']):
        raise PermissionError('pinned independent work association changed')
    if record['status'] == 'queued' and not binding['admissible']:
        raise PermissionError('independent work is closed or paused')
    from hermes_cli.kanban_task_source import project_exists
    project_exists(service.home, binding['project_id'], allow_archived=record['status'] != 'queued')


def edge_task_id(record):
    binding = record.get('work_task_binding')
    return binding['task_id'] if binding else record['document']['task_id']


def validate_work_document(service, policy, key, document, work_task_id):
    if work_task_id is not None:
        _, turn = receiving_main(service, policy, key)
        if document != turn.work["document"]:
            raise PermissionError("work dispatch business document version changed")


def work_attribution(record):
    binding = record.get("work_task_binding")
    if binding is None:
        return {}
    return {"execution_task": {"task_id": binding["task_id"], "native_id": binding["native_id"],
            "profile": binding["profile"], "business_task_id": record["document"]["task_id"]}}
