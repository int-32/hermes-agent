"""Actual non-Linux hosts keep the product's native systemd bypass."""
import pytest
from tools import process_registry


@pytest.mark.platforms('not linux')
def test_native_non_linux_child_does_not_probe_systemd(monkeypatch):
    def unexpected_probe(*args, **kwargs):
        pytest.fail('native non-Linux path probed systemd')
    monkeypatch.setenv('INVOCATION_ID', 'unit-fixture')
    monkeypatch.setattr(process_registry, '_is_supervised_gateway_process', unexpected_probe)
    monkeypatch.setattr(process_registry, '_systemd_run_user_scope_available', unexpected_probe)
    monkeypatch.setattr(process_registry, '_build_systemd_scope_argv', unexpected_probe)
    command = ['fixture-worker']
    result = process_registry.restart_safe_gateway_child_argv(
        command, unit_suffix='native-contract', require_restart_safe_scope=True, outlives_parent=True)
    assert result.mode == 'in_process'
    assert result.argv is command
