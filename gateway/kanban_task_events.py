"""Project committed native dependency phases onto their already bound task card.

The journal remains execution evidence; the card remains the task source. A
projection marker only retries this observation, never execution or delivery.
"""
from datetime import UTC, datetime
import hashlib
import json


PHASE_KINDS = {'queued': 'progress', 'generated': 'progress', 'waiting_children': 'progress',
               'delivered': 'review', 'failed': 'failed', 'ambiguous': 'failed', 'blocked': 'failed'}


def needs_observation(record):
    return (record.get('protocol') == 2 and bool(record.get('task_binding'))
            and record.get('status') in PHASE_KINDS
            and record.get('kanban_event_status') != record['status'])


def publish_phase(service, request_id):
    from gateway.agent_handoff import completion_owner, _HANDOFF_OWNER_LOCK
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_task_source import board_path, source_index, observe_task
    with _HANDOFF_OWNER_LOCK:
        completion_owner(service)
        record = service.journal.get(request_id)
        if not record or not needs_observation(record):
            return
        binding = record['task_binding']
        task_id = record['document']['task_id']
        with kbc.connect_closing(db_path=board_path(service.home)) as conn, kb.write_txn(conn):
            current = source_index(conn).get(task_id)
            if not current or any(current.get(k) != v for k, v in binding.items() if k != 'admissible'):
                raise PermissionError('native phase task source changed')
            identity = [request_id, record['status'], record['document']['sha256']]
            event_id = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
            history = [e.payload for e in kb.list_events(conn, current['native_id'])
                       if e.kind == 'native_task_observed']
            if not any(e['event'].get('producer_event_id') == event_id for e in history):
                event = dict(task_id=task_id, kind=PHASE_KINDS[record['status']],
                    revision=max((e['revision'] for e in history), default=0) + 1,
                    observed_at=datetime.fromtimestamp(record['updated_at'], UTC).isoformat(),
                    needs_user=False, summary='Native dependency stage: ' + record['status']
                        + '. Task goal remains unverified.', producer='native-coordination',
                    producer_event_id=event_id, request_id=request_id, request_status=record['status'])
                observe_task(conn, current['native_id'], event)
        # If this acknowledgement fails after the card COMMIT, the event key
        # above reconciles it on retry without appending a second observation.
        service.journal.update(request_id, kanban_event_status=record['status'])
