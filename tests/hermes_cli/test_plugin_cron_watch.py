"""Real plugin discovery and ledger writers: missing host and identity never grant a wake."""
import json

import pytest


@pytest.fixture
def producer(tmp_path):
    from cron.jobs import create_job
    from cron.executions import create_execution
    from hermes_cli.config import atomic_config_write
    from hermes_cli.plugins import PluginManager
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    job = create_job(prompt='fixture checkpoint', schedule='every 4h', deliver='local')
    # A real claimed attempt is enough for observe-only; do not pretend it executed/completed.
    fire = create_execution(job['id'], source='direct')
    atomic_config_write(home / 'config.yaml', {'plugins': {
        'enabled': ['cron-task-check'], 'entries': {'cron-task-check': {
            'allow_gateway_injection': True, 'settings': {'watch_jobs': {job['id']: 'fixture-task'}}}}}})
    manager = PluginManager()
    manager.discover_and_load()
    try:
        yield home, manager, fire
    finally:
        manager.unload('cron-task-check')


@pytest.mark.parametrize('args, expected', [
    ({'content': 'data'}, 'requires a real canonical cron execution'),
    ({'content': 'data', 'session_key': 'forged', 'session_id': 'forged'}, 'only content'),
    ({'content': 'data', 'execution_id': 'missing'}, 'absent or not running'),
])
def test_untrusted_arguments_do_not_create_an_execution(producer, args, expected):
    from tools.registry import registry
    home, manager, _ = producer
    result = json.loads(registry.dispatch('cron_task_check', args, scope=manager.scope_key))
    assert expected in result['error'] and result.get('completed') is not True, result
    assert not (home / 'runtime/agent_handoff.db').exists()


def test_old_execution_observation_requires_actual_published_host(producer):
    from tools.registry import registry
    home, manager, fire = producer
    result = json.loads(registry.dispatch('cron_task_check', {'content': 'data', 'execution_id': fire['id']},
                                         scope=manager.scope_key))
    assert result == {'error': 'no live gateway host', 'completed': False}
    assert not (home / 'runtime/agent_handoff.db').exists()


def test_immutable_plugin_home_cannot_borrow_callers_profile(producer, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    home, _, fire = producer
    foreign = tmp_path / 'foreign-home'
    foreign.mkdir()
    token = set_hermes_home_override(foreign)
    try:
        manager = PluginManager(scope_key=str(foreign))
        ctx = PluginContext(PluginManifest(name='cron-task-check', key='cron-task-check', source='user'), manager)
    finally:
        reset_hermes_home_override(token)
    assert manager.home_path == foreign
    with pytest.raises(PermissionError, match='another profile home'):
        ctx.request_cron_task('SID and requester claims are untrusted data', execution_id=fire['id'])
    assert not (home / 'runtime/agent_handoff.db').exists()
