"""I2: actual lifecycle interleavings cannot publish private completion for an obsolete owner."""
import asyncio
from contextlib import contextmanager
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from gateway import agent_handoff as handoff, coordination_receipts as receipts, status
from gateway.run_plugin_injection import GatewayPluginInjectionMixin
from gateway.run_shutdown import GatewayShutdownMixin
from tests.gateway.native_source_fixtures import produced_star
from tests.gateway.test_native_task_source_review import submit

pytestmark = pytest.mark.platforms('posix')


@pytest_asyncio.fixture
async def owned(tmp_path, monkeypatch):
    value = produced_star(tmp_path, monkeypatch)
    runner = value.service.runner
    monkeypatch.setattr(status, '_get_gateway_lock_path', lambda: value.home / 'runtime' / 'gateway.lock')
    assert status.acquire_gateway_runtime_lock()
    monkeypatch.setattr(handoff, '_service', None)
    monkeypatch.setattr('hermes_constants.get_routing_process_hermes_home', lambda: value.home)
    parked = asyncio.Event()
    async def watch(self):
        await parked.wait()
    monkeypatch.setattr(handoff.HandoffService, 'watch', watch)
    handoff.install_handoff_service(runner)
    value.service = handoff._service
    runner._restart_requested = False
    runner._clear_plugin_message_injector = MethodType(GatewayPluginInjectionMixin._clear_plugin_message_injector, runner)
    runner._mark_api_runs_shutdown_requested = lambda: None
    runner._cancel_secondary_profile_reconnect_tasks = AsyncMock()
    runner._notify_active_sessions_of_shutdown = AsyncMock()
    runner._active_work_count = lambda: 1
    runner._update_runtime_status = lambda *a: None
    yield value
    handoff.clear_handoff_service(runner)
    for task in list(runner._background_tasks):
        task.cancel()
    await asyncio.gather(*list(runner._background_tasks), return_exceptions=True)
    status.release_gateway_runtime_lock()
    value.store.close_all_db_handles()


async def lifecycle(value, operation):
    runner = value.service.runner
    if operation == 'teardown':
        ctx = SimpleNamespace(started_at=0, elapsed=lambda: 0)
        await GatewayShutdownMixin._stop_begin_teardown(runner, ctx)
        return
    def reacquire():
        status.release_gateway_runtime_lock()
        assert status.acquire_gateway_runtime_lock()
    from gateway.runtime_ownership import process_ownership
    def profile_release():
        process_ownership.release(status._get_process_hermes_home())
    def profile_reacquire():
        profile_release()
        assert status.acquire_gateway_runtime_lock()
    operations = {'clear': lambda: handoff.clear_handoff_service(runner),
                  'replace': lambda: handoff.install_handoff_service(runner),
                  'release': status.release_gateway_runtime_lock,
                  'release-reacquire': reacquire, 'profile-release': profile_release,
                  'profile-reacquire': profile_reacquire, 'profile-close': process_ownership.close}
    operations[operation]()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['clear', 'replace', 'teardown', 'release', 'release-reacquire',
                                       'profile-release', 'profile-reacquire', 'profile-close'])
async def test_completion_cannot_publish_after_actual_lifecycle_during_native_await(owned, monkeypatch, operation):
    value, turns, returned, refused = owned, [], [], []
    service = value.service
    original = receipts.native_endpoint
    changed = False
    async def after_endpoint(*args, **kwargs):
        nonlocal changed
        result = await original(*args, **kwargs)
        if not changed:
            changed = True
            await lifecycle(value, operation)
        return result
    async def offline_turn(event):
        turn = receipts.DOCUMENT_TURN.get()
        turns.append(turn)
        monkeypatch.setattr(receipts, 'native_endpoint', after_endpoint)
        entry = value.entries['default-task']
        try:
            returned.append(await receipts.complete_turn(service, entry.session_key, entry.session_id,
                value.home, turn.work['request_id'], 'completed'))
        except PermissionError:
            refused.append(True)
        raise asyncio.CancelledError()
    service.runner.adapter.handle_message = offline_turn
    await submit(value)
    with pytest.raises(asyncio.CancelledError):
        await service.tick()
    record = service.journal.get('review-request-001')
    print(json.dumps(dict(operation=operation, changed=changed, returned=returned, refused=refused,
                          completion=turns[0].completion, record=record)))
    assert changed and refused and not returned
    assert turns[0].completion is None and not record.get('acknowledgements')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['clear', 'release'])
async def test_completion_sql_update_losing_owner_rolls_back_before_memory_publication(owned, monkeypatch, operation):
    value, turns, refused = owned, [], []
    service = value.service
    original = service.journal.connect
    @contextmanager
    def intercept():
        with original() as db:
            class Connection:
                def execute(self, sql, *args):
                    result = db.execute(sql, *args)
                    if sql.startswith('UPDATE requests SET record='):
                        if operation == 'clear':
                            handoff.clear_handoff_service(service.runner)
                        else:
                            status.release_gateway_runtime_lock()
                    return result
            yield Connection()
    async def offline_turn(event):
        turn = receipts.DOCUMENT_TURN.get(); turns.append(turn)
        monkeypatch.setattr(service.journal, 'connect', intercept)
        entry = value.entries['default-task']
        try:
            await receipts.complete_turn(service, entry.session_key, entry.session_id, value.home,
                                         turn.work['request_id'], 'completed')
        except PermissionError:
            refused.append(True)
        raise asyncio.CancelledError()
    service.runner.adapter.handle_message = offline_turn
    await submit(value)
    with pytest.raises(asyncio.CancelledError):
        await service.tick()
    assert refused and turns[0].completion is None
    assert not service.journal.get('review-request-001').get('acknowledgements')


@pytest.mark.asyncio
async def test_accepted_turn_can_settle_during_actual_external_draining(owned):
    value, turns, returned = owned, [], []
    service = value.service
    async def offline_turn(event):
        turn = receipts.DOCUMENT_TURN.get(); turns.append(turn)
        GatewayShutdownMixin._enter_external_drain(service.runner)
        entry = value.entries['default-task']
        returned.append(await receipts.complete_turn(service, entry.session_key, entry.session_id,
            value.home, turn.work['request_id'], 'completed'))
        raise asyncio.CancelledError()
    service.runner.adapter.handle_message = offline_turn
    await submit(value)
    with pytest.raises(asyncio.CancelledError):
        await service.tick()
    assert returned[0]['status'] == 'recorded' and turns[0].completion == 'completed'
    assert service.journal.get('review-request-001')['acknowledgements'][0]['outcome'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['clear', 'release', 'profile-release', 'profile-close'])
async def test_real_concurrent_lifecycle_cannot_split_commit_and_completion(owned, monkeypatch, operation):
    import threading
    from gateway.runtime_ownership import process_ownership
    value, turns, observations = owned, [], []
    service = value.service
    original_lock = handoff._HANDOFF_OWNER_LOCK
    assert original_lock is process_ownership._mutex
    attempting, done = threading.Event(), threading.Event()
    contender = None
    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is contender and not attempting.is_set():
                acquired = original_lock.acquire(blocking=False)
                observations.append(dict(critical_section_protected=not acquired))
                attempting.set()
                if not acquired:
                    original_lock.acquire()
            else:
                original_lock.acquire()
            return self
        def __exit__(self, *args):
            original_lock.release()
    observed_lock = ObservedLock()
    monkeypatch.setattr(handoff, '_HANDOFF_OWNER_LOCK', observed_lock)
    monkeypatch.setattr(process_ownership, '_mutex', observed_lock)
    original = service.journal.connect
    def clear_owner():
        try:
            operations = {'clear': lambda: handoff.clear_handoff_service(service.runner),
                          'release': status.release_gateway_runtime_lock,
                          'profile-release': lambda: process_ownership.release(status._get_process_hermes_home()),
                          'profile-close': process_ownership.close}
            operations[operation]()
            observations.append(dict(completion=turns[0].completion,
                acknowledgements=service.journal.get('review-request-001').get('acknowledgements')))
        finally:
            done.set()
    @contextmanager
    def intercept():
        with original() as db:
            class Connection:
                def execute(self, sql, *args):
                    nonlocal contender
                    result = db.execute(sql, *args)
                    if sql.startswith('UPDATE requests SET record=') and contender is None:
                        contender = threading.Thread(target=clear_owner)
                        contender.start()
                        assert attempting.wait(10)
                    return result
            yield Connection()
    async def offline_turn(event):
        turn = receipts.DOCUMENT_TURN.get(); turns.append(turn)
        monkeypatch.setattr(service.journal, 'connect', intercept)
        entry = value.entries['default-task']
        result = await receipts.complete_turn(service, entry.session_key, entry.session_id, value.home,
                                             turn.work['request_id'], 'completed')
        assert result['status'] == 'recorded'
        raise asyncio.CancelledError()
    service.runner.adapter.handle_message = offline_turn
    await submit(value)
    try:
        with pytest.raises(asyncio.CancelledError):
            await service.tick()
        assert await asyncio.to_thread(done.wait, 10)
        assert observations[0]['critical_section_protected']
        assert observations[1]['completion'] == 'completed' and observations[1]['acknowledgements']
    finally:
        if contender is not None:
            await asyncio.to_thread(contender.join, 10)
            assert not contender.is_alive()


@pytest.mark.asyncio
async def test_failed_ack_transaction_does_not_publish_memory_completion(owned, monkeypatch):
    import sqlite3
    value, turns = owned, []
    service = value.service
    original = service.journal.connect
    @contextmanager
    def failed_commit():
        with original() as db:
            yield db
            raise sqlite3.OperationalError('isolated commit refusal')
    async def offline_turn(event):
        turn = receipts.DOCUMENT_TURN.get(); turns.append(turn)
        entry = value.entries['default-task']
        with monkeypatch.context() as patch:
            patch.setattr(service.journal, 'connect', failed_commit)
            with pytest.raises(sqlite3.OperationalError):
                await receipts.complete_turn(service, entry.session_key, entry.session_id, value.home,
                                             turn.work['request_id'], 'completed')
        raise asyncio.CancelledError()
    service.runner.adapter.handle_message = offline_turn
    await submit(value)
    with pytest.raises(asyncio.CancelledError):
        await service.tick()
    assert turns[0].completion is None
    assert not service.journal.get('review-request-001').get('acknowledgements')


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_caller', [False, True])
async def test_actual_teardown_after_sql_commit_preserves_ack_and_memory(owned, monkeypatch, cancel_caller):
    import threading
    value, turns, returned, refused, observations, observer_errors = owned, [], [], [], [], []
    service = value.service
    loop_thread = threading.current_thread()
    committed, contending, release = threading.Event(), threading.Event(), threading.Event()
    original_lock, original_connect = handoff._HANDOFF_OWNER_LOCK, service.journal.connect

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is loop_thread and committed.is_set() and not contending.is_set():
                acquired = original_lock.acquire(blocking=False)
                observations.append(dict(real_lock_contended=not acquired, running=service.runner._running))
                contending.set()
                if not acquired:
                    original_lock.acquire()
            else:
                original_lock.acquire()
            return self

        def __exit__(self, *args):
            original_lock.release()

    @contextmanager
    def after_real_commit():
        updated = False
        with original_connect() as db:
            class Connection:
                def execute(self, sql, *args):
                    nonlocal updated
                    result = db.execute(sql, *args)
                    updated |= sql.startswith('UPDATE requests SET record=')
                    return result
            yield Connection()
        if updated:
            committed.set()
            assert release.wait(10), 'setup: independent observer did not release committed writer'

    def observe_contention():
        try:
            assert contending.wait(10), 'setup: actual teardown did not contend for owner lock'
            # A separate SQLite connection sees the durable COMMIT, not writer-local data.
            with original_connect() as db:
                raw = db.execute('SELECT record FROM requests WHERE id=?', ('review-request-001',)).fetchone()[0]
            observations.append(dict(durable_ack=json.loads(raw).get('acknowledgements'),
                                     completion_before_release=turns[0].completion))
        except BaseException as error:
            observer_errors.append(repr(error))
        finally:
            release.set()

    async def offline_turn(event):
        turn = receipts.DOCUMENT_TURN.get(); turns.append(turn)
        entry = value.entries['default-task']
        monkeypatch.setattr(handoff, '_HANDOFF_OWNER_LOCK', ObservedLock())
        monkeypatch.setattr(service.journal, 'connect', after_real_commit)
        writer = asyncio.create_task(receipts.complete_turn(service, entry.session_key, entry.session_id,
            value.home, turn.work['request_id'], 'completed'))
        observer = threading.Thread(target=observe_contention)
        try:
            assert await asyncio.to_thread(committed.wait, 10), 'setup: writer did not COMMIT'
            observer.start()
            if cancel_caller:
                writer.cancel()
            # Runs on the real gateway loop and follows its actual injector -> service clear chain.
            await lifecycle(value, 'teardown')
            try:
                returned.append(await writer)
            except (PermissionError, asyncio.CancelledError) as error:
                refused.append(type(error).__name__)
        finally:
            release.set()
            if observer.ident is not None:
                await asyncio.to_thread(observer.join, 10)
                assert not observer.is_alive(), 'setup: observer failed to terminate'
            await asyncio.gather(writer, return_exceptions=True)
        raise asyncio.CancelledError()

    service.runner.adapter.handle_message = offline_turn
    await submit(value)
    with pytest.raises(asyncio.CancelledError):
        await service.tick()
    record = service.journal.get('review-request-001')
    result = dict(cancel_caller=cancel_caller, observations=observations, returned=returned, refused=refused,
                  completion=turns[0].completion, acknowledgements=record.get('acknowledgements'))
    print(json.dumps(result))
    assert not observer_errors, observer_errors
    assert observations[0]['real_lock_contended'] and observations[1]['durable_ack']
    assert observations[1]['completion_before_release'] is None
    assert record['acknowledgements'] and turns[0].completion == 'completed', result
    assert observations[0]['running'] and not service.runner._running
    assert (refused == ['CancelledError'] and not returned) if cancel_caller else (
        not refused and returned[0]['status'] == 'recorded')
