"""Ordinary gateway bootstrap; only Discord network I/O is inert."""
import asyncio
import json
import traceback
from pathlib import Path
from types import MethodType

from gateway.config import Platform
from gateway.run import GatewayRunner, load_gateway_config, _profile_runtime_scope
from hermes_cli.config import atomic_config_write, load_config_readonly
from hermes_cli import kanban_db, kanban_db_connect, projects_db
from hermes_cli.kanban_task_source import accept_task
from hermes_constants import get_hermes_home
from plugins.platforms.discord.adapter import DiscordAdapter
from gateway.platforms.event import MessageEvent
from gateway.platforms.base import SendResult
from tests.waiting import async_wait_until


def prepare():
    home = get_hermes_home()
    if (home / 'observe-restart').exists():
        assert (home / 'native-fixture.json').exists()
        return
    from hermes_cli.profiles import create_profile
    peer = create_profile('peer', no_alias=True, no_skills=True)
    assert peer == home / 'profiles/peer'
    parent_config = load_config_readonly()
    with _profile_runtime_scope(peer):
        peer_config = load_config_readonly()
        peer_config.setdefault('model', {}).update(parent_config['model'])
        peer_config.setdefault('plugins', {})['enabled'] = []
        atomic_config_write(peer / 'config.yaml', peer_config)
    with (peer / '.env').open('a') as env:
        env.write('OPENAI_API_KEY=sk-fake-e2e\nDISCORD_ALLOWED_USERS=42\n')
    runner = GatewayRunner(load_gateway_config())
    adapter = DiscordAdapter(runner.config.platforms[Platform.DISCORD])
    runner.adapters = {Platform.DISCORD: adapter}
    entries, sources = {}, {}
    for name, chat, kind in [('main', '100', 'group'), ('peer', '101', 'group'), ('task', '200', 'thread')]:
        fields = dict(chat_id=chat, chat_type=kind, user_id='42', scope_id='99')
        if kind == 'thread':
            fields.update(thread_id=chat, parent_chat_id='300')
        source = adapter.build_source(**fields)
        runner._canonicalize(source)
        with _profile_runtime_scope(peer if name == 'peer' else home):
            entry = runner.session_store.get_or_create_session(source)
        entries[name], sources[name] = entry, source
    mains = {profile: dict(session_key=entries[name].session_key, session_id=entries[name].session_id,
                           chat_id=entries[name].origin.chat_id)
             for profile, name in [('default', 'main'), ('peer', 'peer')]}
    assert sources['peer'].profile == 'peer'
    if (home / 'scenario').read_text() == 'route-mismatch':
        mains['default']['chat_id'] = '999'
    doc = home / 'documents/project/watch-task/HANDOFF.md'
    doc.parent.mkdir(parents=True)
    doc.write_text('Fixture task check only. Read this file and return a private completion.')
    policy = dict(enabled=True, execution_enabled=True, coordination=dict(
        enabled=True, owner_user_id='42', guild_id='99', handoff_root=str(home / 'documents'),
        mains=mains, task_parents={'default': ['300']}))
    kanban_db.create_board('registry', tracking_only=True)
    with projects_db.connect_closing() as conn:
        project = projects_db.create_project(conn, name='Fixture', slug='fixture', primary_path=str(home))
    with kanban_db_connect.connect_closing(board='registry') as conn:
        native_id = accept_task(conn, policy=policy['coordination'], source=sources['task'],
            session_key=entries['task'].session_key, session_id=entries['task'].session_id,
            profile_home=home, task_id='watch-task', document_project='project', project_id=project, title='Isolated fixture task')
    config = load_config_readonly()
    config['agent_handoff'] = policy
    atomic_config_write(home / 'config.yaml', config)
    (home / 'native-fixture.json').write_text(json.dumps(dict(
        mains=mains, task=entries['task'].to_dict(), native_id=native_id, handoff=str(doc))))
    runner.session_store._db.close()


async def connect(self, *, is_reconnect=False):
    async def inbound():
        await async_wait_until(lambda: self.gateway_runner is not None and self.gateway_runner._running, timeout=30)
        if (get_hermes_home() / 'observe-restart').exists():
            return
        for name, chat, kind in [('main', '100', 'group'), ('peer', '101', 'group'), ('task', '200', 'thread')]:
            fields = dict(chat_id=chat, chat_type=kind, user_id='42', scope_id='99')
            if kind == 'thread':
                fields.update(thread_id=chat, parent_chat_id='300')
            event = MessageEvent(text='WARM_' + name, source=self.build_source(**fields), message_id='warm-' + name)
            await self.handle_message(event)
        from gateway.session_authorities import authority_for_home
        from hermes_state_runtime import list_session_admissions
        peer = get_hermes_home() / 'profiles/peer'
        authority = authority_for_home(self.gateway_runner, peer)
        await async_wait_until(lambda: any(r['request_id'] == 'warm-peer' and r['status'] == 'terminal'
            for live in authority.sessions.values()
            for r in list_session_admissions(authority.db, session_id=authority.logical_owner(
                self.gateway_runner.session_store.peek_session_id(live.route)), pending_only=False)), timeout=35)
        from gateway.agent_handoff import _service
        from gateway.agent_coordination import coordination_policy, native_endpoint
        source, sid = await native_endpoint(_service, coordination_policy(_service), 'main:peer')
        (get_hermes_home() / 'peer-proof.json').write_text(json.dumps({'source': source.to_dict(), 'session_id': sid}))
        (get_hermes_home() / 'native-warm').touch()
        if (get_hermes_home() / 'scenario').read_text() == 'complete-busy':
            await async_wait_until(lambda: (get_hermes_home() / 'hold-main').exists(), timeout=30)
            await self.handle_message(MessageEvent(text='HOLD_MAIN', source=self.build_source(
                chat_id='100', chat_type='group', user_id='42', scope_id='99'), message_id='hold-main'))
        await async_wait_until(lambda: (get_hermes_home() / 'query-status').exists(), timeout=90)
        await self.handle_message(MessageEvent(text='QUERY_STATUS', source=self.build_source(
            chat_id='100', chat_type='group', user_id='42', scope_id='99'), message_id='query-status'))
        await continuity(self)
    self._fixture_inbound = asyncio.create_task(inbound())
    home = get_hermes_home()
    def finished(task):
        if not task.cancelled() and (error := task.exception()) is not None:
            (home / 'fixture-error').write_text(''.join(traceback.format_exception(error)))
    self._fixture_inbound.add_done_callback(finished)
    return True


async def continuity(adapter):
    home, runner = get_hermes_home(), adapter.gateway_runner
    fixture = json.loads((home / 'native-fixture.json').read_text())
    main_key = fixture['mains']['default']['session_key']
    await async_wait_until(lambda: (home / 'compress-main').exists(), timeout=90)
    await async_wait_until(lambda: main_key not in adapter._active_sessions, timeout=30)
    old = runner.session_store.peek_session_id(main_key)
    source = adapter.build_source(chat_id='100', chat_type='group', user_id='42', scope_id='99')
    await adapter.handle_message(MessageEvent(text='/compress', source=source, message_id='compress-main'))
    await async_wait_until(lambda: runner.session_store.peek_session_id(main_key) != old, timeout=35)
    (home / 'compressed.json').write_text(json.dumps(dict(before=old, after=runner.session_store.peek_session_id(main_key))))
    await async_wait_until(lambda: (home / 'query-compressed').exists(), timeout=30)
    await adapter.handle_message(MessageEvent(text='QUERY_STATUS_COMPRESSED', source=adapter.build_source(
        chat_id='100', chat_type='group', user_id='42', scope_id='99'), message_id='query-compressed'))
    await async_wait_until(lambda: (home / 'reset-target').exists(), timeout=30)
    target = (home / 'reset-target').read_text()
    fields = dict(chat_id='100', chat_type='group', user_id='42', scope_id='99')
    key = main_key
    if target == 'task':
        fields.update(chat_id='200', chat_type='thread', thread_id='200', parent_chat_id='300')
        key = fixture['task']['session_key']
    await async_wait_until(lambda: main_key not in adapter._active_sessions and key not in adapter._active_sessions, timeout=30)
    old = runner.session_store.peek_session_id(key)
    await adapter.handle_message(MessageEvent(text='/new', source=adapter.build_source(**fields), message_id='reset-target'))
    from tools.slash_confirm import get_pending
    await async_wait_until(lambda: get_pending(key) is not None, timeout=20)
    await adapter.handle_message(MessageEvent(text='/approve', source=adapter.build_source(**fields), message_id='approve-reset'))
    await async_wait_until(lambda: runner.session_store.peek_session_id(key) != old, timeout=30)
    (home / 'reset-ready').touch()
    await async_wait_until(lambda: (home / 'query-reset').exists(), timeout=30)
    await adapter.handle_message(MessageEvent(text='QUERY_STATUS_RESET', source=adapter.build_source(
        chat_id='100', chat_type='group', user_id='42', scope_id='99'), message_id='query-reset'))


async def send(self, chat_id, content, **kwargs):
    home = get_hermes_home()
    with (home / 'transport.jsonl').open('a') as out:
        out.write(json.dumps(dict(chat_id=chat_id, text=content)) + '\n')
    return SendResult(success=True, message_id='inert-platform-receipt')


if __name__ == '__main__':
    prepare()
    from run_agent import AIAgent
    original_init = AIAgent.__init__
    def observed_init(self, *args, **kwargs):
        with (get_hermes_home() / 'constructors.jsonl').open('a') as log:
            log.write(json.dumps({'session_id': kwargs.get('session_id')}) + '\n')
        original_init(self, *args, **kwargs)
    AIAgent.__init__ = observed_init
    from hermes_cli.plugin_host import PluginHost
    original_encode = PluginHost._encode_for_host
    def reply(self, value):
        # Transport fault only: validation and the real SQLite commit already finished.
        if (isinstance(value, str) and '"admission_id"' in value
                and not (get_hermes_home() / 'observe-restart').exists()
                and not getattr(self, '_fixture_lost_reply', False)):
            self._fixture_lost_reply = True
            raise TimeoutError('fixture discarded the post-COMMIT plugin response')
        return original_encode(self, value)
    PluginHost._encode_for_host = reply
    original_factory = GatewayRunner._create_adapter
    def fixture_adapter(self, platform, config):
        adapter = original_factory(self, platform, config)
        if adapter is not None and platform == Platform.DISCORD:
            adapter.connect = MethodType(connect, adapter)
            adapter.send = MethodType(send, adapter)
        return adapter
    GatewayRunner._create_adapter = fixture_adapter
    from gateway.run import main
    main()
