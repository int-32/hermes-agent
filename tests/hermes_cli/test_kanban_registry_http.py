"""Real native HTTP/auth/WS fixture, isolated from installed services and homes."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

import httpx
import pytest
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus

from tests.waiting import wait_until

SERVER = '''
import os
from pathlib import Path
from hermes_cli.config import atomic_config_write, save_env_value
from plugins.dashboard_auth.basic import hash_password
home=Path(os.environ['HERMES_HOME']); home.mkdir(parents=True,exist_ok=True)
atomic_config_write(home/'config.yaml', {'plugins':{'enabled':['kanban','basic']},'dashboard':{'public_url':'https://kanban.fixture.test','basic_auth':{'username':'fixture','password_hash':hash_password('offline-fixture-only')}}})
save_env_value('HERMES_DASHBOARD_BASIC_AUTH_SECRET', 'fixture-only-32-byte-session-key!!')
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
kb.create_board('registry', tracking_only=True)
with kbc.connect_closing(board='registry') as conn:
    tid=kb.create_task(conn,title='HTTP fixture card',body='Offline fixture details',initial_status='blocked',board='registry')
    kb.observe_tracking_task(conn,tid,'running',actor='fixture',evidence={'source':'offline'})
from hermes_cli.plugins import discover_plugins
discover_plugins()
from hermes_cli.web_server import start_server
start_server(host='127.0.0.1',port=0,open_browser=False,isolated=True)
'''


@pytest.fixture
def http_service(tmp_path):
    root = Path(__file__).parents[2]
    home = tmp_path / 'home'
    env = {'PATH': os.pathsep.join((str(Path(sys.executable).parent), '/usr/bin', '/bin', '/usr/sbin', '/sbin')),
           'HOME': str(home), 'HERMES_HOME': str(home / 'hermes'), 'HERMES_RUNTIME_DIR': str(home / 'runtime'),
           'HERMES_DISABLE_LAZY_INSTALLS': '1', 'PYTHONPATH': str(root), 'NO_PROXY': '*', 'NO_COLOR': '1'}
    log = tmp_path / 'server.log'
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, '-c', SERVER], cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
    try:
        ready = lambda: re.search(r'HERMES_DASHBOARD_READY port=(\d+)', log.read_text())
        assert wait_until(lambda: ready() or process.poll() is not None, timeout=45), log.read_text()
        match = ready()
        assert match, log.read_text()
        yield int(match[1])
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_native_http_password_gate_origin_and_single_use_ws(http_service):
    port = http_service
    headers = {'Host': 'kanban.fixture.test', 'Origin': 'https://kanban.fixture.test', 'X-Forwarded-Proto': 'https'}
    with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers=headers, trust_env=False) as client:
        assert client.get('/api/plugins/kanban/board?board=registry').status_code == 401
        assert client.get('/kanban').status_code == 302
        assert client.get('/login').status_code == 200
        payload = {'provider': 'basic', 'username': 'fixture', 'password': 'wrong'}
        assert client.post('/auth/password-login', json=payload).status_code == 401
        payload['password'] = 'offline-fixture-only'
        login = client.post('/auth/password-login', json=payload)
        assert login.status_code == 200
        assert all('Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=lax' in cookie for cookie in login.headers.get_list('set-cookie'))
        # Genuine cookies minted for the HTTPS origin; this local HTTP socket
        # represents the trusted TLS reverse proxy, not a second auth scheme.
        client.headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in client.cookies.items())
        assert '__HERMES_SESSION_TOKEN__="' not in client.get('/kanban').text
        board = client.get('/api/plugins/kanban/board?board=registry').json()
        assert board['tracking_only'] is True
        task = next(t for col in board['columns'] for t in col['tasks'])
        detail = client.get(f'/api/plugins/kanban/tasks/{task["id"]}?board=registry').json()
        assert detail['task']['status'] == 'running'
        assert not detail['runs']
        preflight = client.options('/api/plugins/kanban/tasks/' + task['id'], headers={'Origin': 'https://other.fixture.test', 'Access-Control-Request-Method': 'PATCH'})
        assert preflight.status_code == 400
        assert client.get('/kanban', headers={'Host': 'other.fixture.test'}).status_code == 400
        ticket = client.post('/api/auth/ws-ticket').json()['ticket']
        uri = f'ws://kanban.fixture.test:{port}/api/plugins/kanban/events?board=registry&since=0&ticket={ticket}'
        def open_ws(url, origin='https://kanban.fixture.test'):
            return connect(url, sock=socket.create_connection(('127.0.0.1', port)), origin=origin, proxy=None, open_timeout=5)
        with open_ws(uri) as ws:
            event = json.loads(ws.recv(timeout=5))
            assert event['events'] and event['cursor'] >= board['latest_event_id']
        with pytest.raises(InvalidStatus):
            open_ws(uri)
        fresh = client.post('/api/auth/ws-ticket').json()['ticket']
        with pytest.raises(InvalidStatus):
            open_ws(uri.replace(ticket, fresh), origin='https://other.fixture.test')
        fresh = client.post('/api/auth/ws-ticket').json()['ticket']
        with open_ws(uri.replace(ticket, fresh)) as ws:
            assert json.loads(ws.recv(timeout=5))['cursor'] == event['cursor']
