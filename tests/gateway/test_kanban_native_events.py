"""Native acceptance and dependency phases feed the existing notification cursor."""
import json
import asyncio

import pytest

from gateway.agent_coordination import submit_document
from gateway.config import GatewayConfig, Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from hermes_cli import kanban_source
from hermes_cli.kanban_task_source import read_sources
from tests.gateway.test_agent_coordination import make_star

pytest_plugins = ('tests.hermes_cli.test_kanban_source',
                  'tests.gateway.test_coordination_native_turn_binding')


def test_persisted_source_acceptance_subscribes_its_actual_thread(intake, capsys):
    home, _project, source, _entry, args, _doc = intake
    assert kanban_source.main(args) == 0
    tid = json.loads(capsys.readouterr().out)['native_id']
    with kbc.connect_closing(board='registry') as conn:
        subs = kbn.list_notify_subs(conn)
        assert len(subs) == 1
        sub = subs[0]
        assert (sub['task_id'], sub['chat_id'], sub['thread_id']) == (tid, source.chat_id, source.thread_id)
        assert sub['notifier_profile'] == 'default'
        assert sub['delivery_mode'] == 'notify'
        assert sub['delivery_metadata']['parent_chat_id'] == source.parent_chat_id
        assert sub['delivery_metadata']['scope_id'] == source.scope_id
        assert kb.list_runs(conn, tid) == []
    assert read_sources(home)['example-research']['native_id'] == tid
    assert kanban_source.main(args) == 0
    capsys.readouterr()
    with kbc.connect_closing(board='registry') as conn:
        assert len(kbn.list_notify_subs(conn)) == 1


class Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))


def notifier_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._primary_profile_name = 'default'
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_sub_fail_counts = {}
    return runner


def phase_events(tid):
    with kbc.connect_closing(board='registry') as conn:
        return [e for e in kb.list_events(conn, tid)
                if e.kind == 'native_task_observed' and e.payload['event'].get('producer') == 'native-coordination']


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
async def test_bound_coordinate_tool_is_an_actual_event_producer(native_call, record_testsuite_property):
    from gateway.session_context import native_model_turn_scope
    from tools.registry import registry
    _gateway, _runner, context, service, args = native_call
    service.runner._gateway_loop = asyncio.get_running_loop()
    with native_model_turn_scope(service.runner, context.source, context.session_key, context.session_id):
        result = await asyncio.to_thread(registry.dispatch, 'coordinate_session', args,
                                         session_id=context.session_id)
    assert json.loads(result)['status'] == 'queued'
    tid = read_sources(service.home)['task-one']['native_id']
    events = phase_events(tid)
    assert len(events) == 1
    assert events[0].payload['event']['request_id'] == args['request_id']
    assert events[0].payload['event']['request_status'] == 'queued'
    record_testsuite_property('bound_coordinate_producer', json.dumps({
        'tool': 'coordinate_session', 'request_id': args['request_id'], 'card': tid,
        'native_event_id': events[0].id, 'status': json.loads(result)['status']}))


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
async def test_real_native_request_phases_reach_original_subscription_without_observe(
        tmp_path, monkeypatch, record_testsuite_property):
    service, doc = make_star(tmp_path, monkeypatch)
    tid = read_sources(service.home)['task-one']['native_id']
    args = (service, 'default-main', 'default-main-sid', service.runner.home / 'default',
            'event-chain-001', 'task-one', str(doc), 'task:task-one')
    await submit_document(*args)
    assert [e.payload['event']['request_status'] for e in phase_events(tid)] == ['queued']
    await service.tick()
    assert service.journal.get('event-chain-001')['status'] == 'delivered'
    events = phase_events(tid)
    assert [e.payload['event']['request_status'] for e in events] == ['queued', 'generated', 'delivered']
    assert all(e.payload['goal_status'] == 'unverified' for e in events)
    native_turns = len(service.runner.received)
    await submit_document(*args)
    await service.tick()
    assert phase_events(tid) == events
    assert len(service.runner.received) == native_turns == 2

    adapter = Adapter()
    runner = notifier_runner(adapter)
    deliveries = _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
    assert any(d['sub']['task_id'] == tid and d['events'] for d in deliveries)
    for delivery in deliveries:
        await _KanbanNotification(runner, delivery, platform_cls=Platform, sub_fail_counts={}).deliver()
    messages = [m for m in adapter.sent if m[0] == 'default-task']
    assert messages and all(m[2]['thread_id'] == 'default-task' for m in messages)
    assert not _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
    assert len(service.runner.received) == native_turns
    with kbc.connect_closing(board='registry') as conn:
        assert kb.board_tracking_only(conn)
        assert kb.list_runs(conn, tid) == []
        assert kb.get_task(conn, tid).completed_at is None
        assert len(read_sources(service.home)) == 2
    record_testsuite_property('native_event_connection', json.dumps({
        'card': tid, 'task_id': 'task-one', 'original_thread': 'default-task',
        'request_id': 'event-chain-001', 'producer_statuses': [e.payload['event']['request_status'] for e in events],
        'consumer': _notifier_collect.__module__, 'consumer_thread_messages': len(messages),
        'private_turn_receipts_before_notifications': native_turns,
        'private_turn_receipts_after_notifications': len(service.runner.received),
        'tracking_only': True, 'native_runs': 0, 'retry_added_events': 0}))


def test_accept_subscription_failure_rolls_back_card_binding_and_subscription(intake, capsys, monkeypatch):
    _home, _project, _source, _entry, args, _doc = intake
    add = kbn.add_notify_sub

    def fail_after_insert(*a, **kw):
        add(*a, **kw)
        raise OSError('isolated subscription write failure')

    monkeypatch.setattr(kbn, 'add_notify_sub', fail_after_insert)
    assert kanban_source.main(args) == 2
    capsys.readouterr()
    with kbc.connect_closing(board='registry') as conn:
        assert kb.list_tasks(conn) == []
        assert kbn.list_notify_subs(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0


def test_reaccept_preserves_explicit_notification_mode_and_durable_cursor(intake, capsys):
    _home, _project, source, _entry, args, _doc = intake
    assert kanban_source.main(args) == 0
    tid = json.loads(capsys.readouterr().out)['native_id']
    with kbc.connect_closing(board='registry') as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform='discord', chat_id=source.chat_id,
                           thread_id=source.thread_id, delivery_mode='notify+wake')
        before = kbn.list_notify_subs(conn)[0]
    assert kanban_source.main(args) == 0
    capsys.readouterr()
    with kbc.connect_closing(board='registry') as conn:
        assert kbn.list_notify_subs(conn)[0] == before


def test_existing_auto_subscription_opt_out_is_respected(intake, capsys):
    home, _project, _source, _entry, args, _doc = intake
    config_path = home / 'config.yaml'
    config = json.loads(config_path.read_text())
    config['kanban'] = {'auto_subscribe_on_create': False}
    config_path.write_text(json.dumps(config))
    assert kanban_source.main(args) == 0
    capsys.readouterr()
    with kbc.connect_closing(board='registry') as conn:
        assert kbn.list_notify_subs(conn) == []


@pytest.mark.parametrize('kind,expected', [('progress', None), ('accept', None), ('idle', None),
                                          ('review', 'review_requested'), ('completed', 'review_requested'),
                                          ('failed', 'blocked'), ('blocked', 'blocked')])
def test_only_decision_events_are_eligible_for_an_explicitly_enabled_wake(kind, expected):
    from types import SimpleNamespace
    from gateway.kanban_watchers_notifier import _wake_kind
    event = SimpleNamespace(kind='native_task_observed', payload={'event': {'kind': kind}})
    assert _wake_kind(event) == expected


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
@pytest.mark.parametrize('failed_phase', ['queued', 'delivered'])
async def test_projection_ack_failure_retries_only_observation_after_journal_reopen(tmp_path, monkeypatch, failed_phase):
    from gateway.agent_handoff import HandoffJournal
    service, doc = make_star(tmp_path, monkeypatch)
    tid = read_sources(service.home)['task-one']['native_id']
    update = service.journal.update

    def fail_ack(request_id, **changes):
        if changes.get('kanban_event_status') == failed_phase:
            raise OSError('isolated lost projection acknowledgement')
        return update(request_id, **changes)

    monkeypatch.setattr(service.journal, 'update', fail_ack)
    await submit_document(service, 'default-main', 'default-main-sid', service.runner.home / 'default',
                          'ack-retry-0001', 'task-one', str(doc), 'task:task-one')
    await service.tick()
    assert service.journal.get('ack-retry-0001')['status'] == 'delivered'
    assert len(service.runner.received) == 2
    original = phase_events(tid)
    service.journal = HandoffJournal(service.journal.path)
    await service.tick()
    assert phase_events(tid) == original
    assert len(service.runner.received) == 2
    assert service.journal.get('ack-retry-0001')['kanban_event_status'] == 'delivered'
    assert service.journal.pending() == []


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
async def test_user_pause_remains_authoritative_and_does_not_execute(tmp_path, monkeypatch):
    service, doc = make_star(tmp_path, monkeypatch)
    tid = read_sources(service.home)['task-one']['native_id']
    await submit_document(service, 'default-main', 'default-main-sid', service.runner.home / 'default',
                          'paused-event-01', 'task-one', str(doc), 'task:task-one')
    with kbc.connect_closing(board='registry') as conn:
        assert kb.block_task(conn, tid, reason='User pause')
    await service.tick()
    assert not service.runner.received
    with kbc.connect_closing(board='registry') as conn:
        assert kb.get_task(conn, tid).status == 'blocked'
        assert kb.list_runs(conn, tid) == []
    assert phase_events(tid)[-1].payload['manual_status_preserved'] is True


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
async def test_real_service_clear_refuses_late_phase_publication(tmp_path, monkeypatch):
    from gateway.agent_handoff import clear_handoff_service
    from gateway.kanban_task_events import publish_phase
    service, doc = make_star(tmp_path, monkeypatch)
    tid = read_sources(service.home)['task-one']['native_id']
    await submit_document(service, 'default-main', 'default-main-sid', service.runner.home / 'default',
                          'late-phase-001', 'task-one', str(doc), 'task:task-one')
    before = phase_events(tid)
    clear_handoff_service(service.runner)
    with pytest.raises(PermissionError, match='obsolete native owner'):
        publish_phase(service, 'late-phase-001')
    assert phase_events(tid) == before


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
async def test_native_notification_cursor_recovers_platform_failure_without_task_reexecution(tmp_path, monkeypatch):
    service, doc = make_star(tmp_path, monkeypatch)
    tid = read_sources(service.home)['task-one']['native_id']
    await submit_document(service, 'default-main', 'default-main-sid', service.runner.home / 'default',
                          'notify-retry-01', 'task-one', str(doc), 'task:task-one')
    await service.tick()
    adapter = Adapter()
    runner = notifier_runner(adapter)
    failures = {}
    send = adapter.send

    async def disconnected(*args, **kwargs):
        raise OSError('isolated platform outage')

    adapter.send = disconnected
    first = _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
    for delivery in first:
        await _KanbanNotification(runner, delivery, platform_cls=Platform, sub_fail_counts=failures).deliver()
    assert not adapter.sent
    adapter.send = send
    retry = _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
    assert any(d['sub']['task_id'] == tid for d in retry)
    for delivery in retry:
        await _KanbanNotification(runner, delivery, platform_cls=Platform, sub_fail_counts=failures).deliver()
    assert [m for m in adapter.sent if m[0] == 'default-task']
    assert not _notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
    assert len(service.runner.received) == 2
