"""Real plugin discovery/teardown in a fresh process and a disposable home."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.platforms('posix')

CHILD = r'''
import asyncio, hashlib, json, os, shutil, sys
from pathlib import Path
from types import SimpleNamespace
from gateway import agent_handoff
from gateway.control_socket import GatewayControlServer
from hermes_cli.plugins import PluginManager
from tests.waiting import async_wait_until

async def run():
    root, home, defect = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    plugin = home / 'plugins' / 'task-handoff-host'
    shutil.copytree(root / 'plugins' / 'task-handoff-host', plugin)
    files = ['scripts/task_handoff_check.py', 'scripts/task_handoff_native.py', 'scripts/task_handoff_host.py']
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    descriptor = dict(native_source=str(root), native_files={'gateway/agent_handoff.py': digest(root/'gateway/agent_handoff.py')},
                      native_revision='offline-native-pin', bundle_root=str(root),
                      bundle_files={p: digest(root/p) for p in files}, task={'home': str(home), 'task_id': 'fixed-task'})
    if defect == 'native_hash': descriptor['native_files']['gateway/agent_handoff.py'] = 'wrong'
    if defect == 'bundle_hash': descriptor['bundle_files'][files[0]] = 'wrong'
    if defect == 'home': descriptor['task']['home'] = str(home / 'other')
    path = home / 'descriptor.json'
    path.write_text(json.dumps(descriptor))
    config = {'plugins': {'enabled': ['task-handoff-host'], 'isolation': 'in_process',
                         'entries': {'task-handoff-host': {'settings': {'descriptor': str(path)}}}}}
    (home/'config.yaml').write_text(json.dumps(config))
    manager = PluginManager()
    manager._scan_entry_points = lambda: []
    identity = dict(pid=os.getpid(), start_time=1, code_sha='offline-native-pin', hermes_home=str(home))
    control = GatewayControlServer(home, verb_handlers={'identify': lambda: identity})
    assert await control.start()
    # Existing native process object is the fixture boundary. Never construct a
    # gateway, session, handoff service or journal from a client-supplied identity.
    agent_handoff._service = SimpleNamespace(home=home, runner=SimpleNamespace(_running=True, _gateway_loop=asyncio.get_running_loop()))
    try:
        manager.discover_and_load()
        loaded = manager._plugins['task-handoff-host']
        if defect:
            assert loaded.error and not (home/'runtime/task-handoff.sock').exists()
            print(json.dumps({'refused': defect}))
            return
        assert loaded.enabled and loaded.error is None, loaded.error
        from scripts.task_handoff_host import query, socket_path
        assert await async_wait_until(lambda: socket_path(home).exists(), timeout=5)
        result = await asyncio.to_thread(query, home, 'status')
        assert result['receipt_verified'] is False and result['pid'] == os.getpid()
        old_host = next(task for task in asyncio.all_tasks() if task.get_name() == 'task-handoff-host')
        manager.discover_and_load(force=True)
        assert await async_wait_until(lambda: old_host.done() and socket_path(home).exists(), timeout=8)
        result = await asyncio.to_thread(query, home, 'status')
        assert result['state'] == 'native_host_attached'
        manager.unload('task-handoff-host')
        assert await async_wait_until(lambda: not socket_path(home).exists(), timeout=5)
        assert not manager._hooks.get('post_gateway_admission')
        print(json.dumps({'loaded': True, 'reloaded': True, 'unloaded': True, 'receipt_verified': False}))
    finally:
        manager.unload()
        await control.stop()

asyncio.run(run())
'''


@pytest.mark.parametrize('defect', ['', 'native_hash', 'bundle_hash', 'home'])
def test_discovery_pins_owner_host_and_unloads(defect):
    root = Path(__file__).resolve().parents[2]
    # macOS AF_UNIX sun_path needs a short, disposable test directory.
    with tempfile.TemporaryDirectory(prefix='hp-', dir='/tmp') as directory:  # no-tmp: ok -- AF_UNIX sun_path limit
        home = Path(directory).resolve()
        empty = home / 'empty'
        empty.mkdir()
        env = dict(os.environ, HERMES_HOME=str(home), HERMES_BUNDLED_PLUGINS=str(empty))
        child = subprocess.run([sys.executable, '-B', '-c', CHILD, str(root), str(home), defect],
                               env=env, cwd=root, capture_output=True, text=True, timeout=30, check=False)
        assert child.returncode == 0, child.stderr
        result = json.loads(child.stdout.splitlines()[-1])
        if defect:
            assert result == {'refused': defect}
        else:
            assert result == dict(loaded=True, reloaded=True, unloaded=True, receipt_verified=False)
