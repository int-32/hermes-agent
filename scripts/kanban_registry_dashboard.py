"""Prepare native Dashboard settings and a profile .env signing secret.

Uses the official writers only. Run explicitly for an operator-selected home;
never starts services. Keep concurrent external config writers quiescent during
this short apply: native YAML writers do not offer cross-process CAS.
"""
import getpass
import os
import secrets

from hermes_cli import config
from hermes_cli.plugins_cmd import ensure_basic_auth_plugin_enabled_in_config
from hermes_constants import get_hermes_home
from plugins.dashboard_auth.basic import hash_password

SECRET_KEY = 'HERMES_DASHBOARD_BASIC_AUTH_SECRET'


def _config_bytes(path):
    return path.read_bytes() if path.exists() else None


def _apply(expected, username, password_hash):
    path = config.get_config_path()
    env_path = config.get_env_path()
    with config._CONFIG_LOCK, config._env_write_lock(env_path):
        if _config_bytes(path) != expected:
            raise SystemExit('Configuration changed while preparing; nothing overwritten')
        current = config.read_user_config_raw()
        plugins = current.setdefault('plugins', {})
        if 'kanban' in plugins.get('disabled', []):
            raise SystemExit('Kanban explicitly disabled; review policy first')
        plugins['enabled'] = sorted(set(plugins.get('enabled', [])) | {'kanban', 'basic'})
        dashboard = current.setdefault('dashboard', {})
        auth = dashboard.setdefault('basic_auth', {})
        legacy_secret = auth.pop('secret', None)
        if legacy_secret and (not isinstance(legacy_secret, str) or legacy_secret.startswith('${')):
            raise SystemExit('Review the existing signing-secret reference before migration')
        signing_secret = config.load_env().get(SECRET_KEY) or legacy_secret or secrets.token_hex(32)
        config.require_env_writable(SECRET_KEY, 'set')
        config.save_env_value(SECRET_KEY, signing_secret)
        os.chmod(env_path, 0o600)
        # The official .env writer owns its cross-process lock; YAML is a
        # separate file. Never overwrite a config change seen after that write.
        if _config_bytes(path) != expected:
            raise SystemExit('Configuration changed during secret preparation; .env retained, config untouched')
        auth.pop('password', None)
        auth.update(username=username, password_hash=password_hash, session_ttl_seconds=3600)
        dashboard['public_url'] = 'https://kanban.aiyu-ai.com'
        ensure_basic_auth_plugin_enabled_in_config(current)
        config.save_config(current)
        os.chmod(path, 0o600)


def main():
    home = get_hermes_home()
    expected = _config_bytes(config.get_config_path())
    print('Selected HERMES_HOME:', home)
    if input('Type the exact absolute home to apply the reviewed Dashboard config: ') != str(home):
        raise SystemExit('Home not confirmed')
    username = input('Dashboard username: ').strip()
    password = getpass.getpass('New Dashboard password: ')
    if not username or len(password) < 16 or password != getpass.getpass('Repeat password: '):
        raise SystemExit('Nonempty username and matching password of at least 16 characters required')
    _apply(expected, username, hash_password(password))
    print('Native Dashboard settings and profile secret prepared. No service started.')


if __name__ == '__main__':
    main()
