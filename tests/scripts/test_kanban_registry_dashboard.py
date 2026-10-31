"""Deployment preparation uses the profile secret writer and refuses stale config."""
from pathlib import Path
import runpy

import pytest

from hermes_cli import config
from plugins.dashboard_auth import basic

SCRIPT = Path(__file__).parents[2] / 'scripts/kanban_registry_dashboard.py'
SECRET_KEY = 'HERMES_DASHBOARD_BASIC_AUTH_SECRET'


def prepare(home, monkeypatch, mutate=None):
    answers = iter((str(home), 'fixture-admin'))
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    calls = []
    def password(prompt):
        calls.append(prompt)
        if len(calls) == 2 and mutate:
            mutate()
        return 'offline-fixture-long-password'
    monkeypatch.setattr('getpass.getpass', password)
    runpy.run_path(str(SCRIPT), run_name='__main__')


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / 'profile'
    path.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(path))
    config.atomic_config_write(path / 'config.yaml', {'terminal': {'timeout': 314}, 'plugins': {'enabled': ['existing']}})
    config.save_env_value('FIXTURE_OTHER_SECRET', 'keep-fixture-value')
    return path


@pytest.mark.parametrize('legacy_secret', [None, 'offline-legacy-signing-key-at-least-32'])
def test_signing_secret_is_profile_dotenv_only_and_native_provider_can_login(home, monkeypatch, capsys, legacy_secret):
    if legacy_secret:
        config.save_config({'dashboard': {'basic_auth': {'secret': legacy_secret}}}, merge_existing=True)
    prepare(home, monkeypatch)
    raw = config.read_user_config_raw()
    section = raw['dashboard']['basic_auth']
    secret = config.get_env_value_prefer_dotenv(SECRET_KEY)
    assert secret and len(secret) >= 32
    assert 'secret' not in section and 'password' not in section
    assert secret not in (home / 'config.yaml').read_text()
    assert secret not in capsys.readouterr().out
    assert raw['terminal']['timeout'] == 314
    assert set(raw['plugins']['enabled']) == {'existing', 'basic', 'kanban'}
    assert config.get_env_value_prefer_dotenv('FIXTURE_OTHER_SECRET') == 'keep-fixture-value'
    assert (home / '.env').stat().st_mode & 0o777 == 0o600
    session = basic.BasicAuthProvider(**basic._settings()).complete_password_login(username='fixture-admin', password='offline-fixture-long-password')
    assert session.user_id == 'fixture-admin'
    prepare(home, monkeypatch)
    assert config.get_env_value_prefer_dotenv(SECRET_KEY) == secret
    assert basic.BasicAuthProvider(**basic._settings()).verify_session(access_token=session.access_token)


def test_changed_config_while_prompting_is_refused_without_overwrite(home, monkeypatch):
    def change():
        config.save_config({'terminal': {'timeout': 271}}, merge_existing=True)
    with pytest.raises(SystemExit, match='changed'):
        prepare(home, monkeypatch, mutate=change)
    assert config.read_user_config_raw()['terminal']['timeout'] == 271
    assert config.get_env_value_prefer_dotenv(SECRET_KEY) is None


def test_config_change_during_env_write_preserves_concurrent_config(home, monkeypatch):
    writer = config.save_env_value
    def concurrent_write(key, value):
        writer(key, value)
        config.save_config({'terminal': {'timeout': 281}}, merge_existing=True)
    monkeypatch.setattr(config, 'save_env_value', concurrent_write)
    with pytest.raises(SystemExit, match='changed'):
        prepare(home, monkeypatch)
    assert config.read_user_config_raw()['terminal']['timeout'] == 281
    assert 'dashboard' not in config.read_user_config_raw()
    assert config.get_env_value_prefer_dotenv(SECRET_KEY)
