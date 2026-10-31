"""Unavailable canonical auth cannot turn the native event stream public."""
import builtins
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.plugins.test_kanban_dashboard_plugin import _load_plugin_router


@pytest.mark.parametrize('failure', ['import', 'authorization'])
def test_events_reject_when_canonical_auth_is_unavailable(monkeypatch, failure):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix='/api/plugins/kanban')
    original_import = builtins.__import__
    def unavailable(*args, **kwargs):
        raise ImportError('fixture dependency unavailable')
    if failure == 'import':
        def controlled_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'hermes_cli' and fromlist and 'web_server_chat' in fromlist:
                return unavailable()
            return original_import(name, globals, locals, fromlist, level)
        monkeypatch.setattr(builtins, '__import__', controlled_import)
    else:
        import hermes_cli
        stub = SimpleNamespace(_ws_request_is_allowed=lambda ws: True, _ws_auth_ok=unavailable)
        monkeypatch.setitem(sys.modules, 'hermes_cli.web_server_chat', stub)
        monkeypatch.setattr(hermes_cli, 'web_server_chat', stub, raising=False)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/api/plugins/kanban/events?since=0'):
                pytest.fail('event stream accepted without canonical authentication')
        assert denied.value.code == 1008
