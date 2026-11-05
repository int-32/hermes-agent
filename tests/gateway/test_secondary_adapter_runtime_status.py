"""A secondary profile's adapter connect is recorded as ``<profile>:<platform>`` by the runner,
so adapters that never stamp their own status (Slack) still show up in gateway_state.json."""

from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner


def _runner(calls):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._update_platform_runtime_status = lambda platform, **kw: calls.append((platform, kw))
    return runner


def test_secondary_connect_is_recorded_under_profile_key():
    calls = []
    adapter = SimpleNamespace(send_path_degraded=False, DEGRADED_STATUS_MESSAGE="degraded")
    _runner(calls)._mark_secondary_adapter_connected("router", Platform.SLACK, adapter)
    assert calls == [("router:slack", {"platform_state": "connected", "error_code": None, "error_message": None})]


def test_degraded_secondary_connect_is_recorded_as_retrying():
    calls = []
    adapter = SimpleNamespace(send_path_degraded=True, DEGRADED_STATUS_MESSAGE="degraded")
    _runner(calls)._mark_secondary_adapter_connected("router", Platform.SLACK, adapter)
    assert calls[0][1]["platform_state"] == "retrying"
    assert calls[0][1]["error_message"] == "degraded"
