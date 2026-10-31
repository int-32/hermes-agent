"""Explicit agent intake, canonical registry and independent Discord projection."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from scripts import agent_task_intake as intake
from scripts import kanban_registry_native as native
from scripts.kanban_registry_records import RegistryError, encoded, load_config
from tests.scripts.test_kanban_registry_sync import NOW, STAMP

pytest_plugins = ['tests.scripts.test_kanban_registry_native']


@pytest.fixture
def setup_intake(native_intake, tmp_path):
    config, record, cp, rp, _receipt = native_intake
    record['revision'] = 4
    rp.write_text(encoded([record]))
    with native.target_scope(config):
        tid = native.apply(config, [record], now=NOW)['rows'][0]['native_id']
    policy = dict(registry_config=str(cp), records=str(rp), output=str(tmp_path / 'events'),
                  actor_profile='default', actor_home=str(config['profile_home']),
                  cli='/official/hermes', guild='100000000000000003', requester='100000000000000004',
                  bot_id='1560000000000000001', bindings={'example': dict(native_id=tid, project='system',
                  thread=record['thread'], owners=['aiyu-dev', 'aiyu-ops'], source_profiles=['default', 'aiyu-dev', 'aiyu-ops'])})
    event = dict(task_id='example', revision=5, kind='accept', owner='aiyu-dev',
                 source_profile='aiyu-dev', source_session_id='real-session', observed_at=STAMP,
                 executor_ids=['worker-new'], summary='正式接单；沿用原任务。', needs_user=False)
    return policy, event, config, tid


def run_native(config_path, records_path, command):
    cfg = load_config(config_path)
    records = json.loads(records_path.read_text())
    with native.target_scope(cfg):
        return (native.apply if command == 'sync' else native.inspect)(cfg, records, now=NOW)


def submit(policy, event):
    prepared = intake.prepare(policy, event)
    def annotate(_policy, tid, body):
        cfg = load_config(prepared['config'])
        with native.target_scope(cfg), kbc.connect_closing(board='registry') as conn:
            if not any(c.body == body for c in kb.list_comments(conn, tid)):
                kb.add_comment(conn, tid, 'default', body)
    return intake.apply_event(policy, prepared, registry=run_native, annotate=annotate)


def test_formal_acceptance_preserves_identity_and_adds_real_custody(setup_intake):
    policy, event, config, tid = setup_intake
    result = submit(policy, event)
    assert result['native_id'] == tid
    assert result['registration_actor'] == 'default'
    assert result['current_owner'] == 'aiyu-dev'
    assert result['original_owner'] == 'controller'
    assert result['status'] == 'running'
    prepared = intake.prepare(policy, event)
    receipt = json.loads(prepared['receipt'].read_text())
    assert receipt['source_profile'] == 'aiyu-dev'
    assert receipt['source_session_id'] == 'real-session'
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert kb.list_runs(conn, tid) == []
        assert kb.claim_task(conn, tid) is None
        assert kb.get_task(conn, tid).assignee is None


@pytest.mark.parametrize('kind,expected', [('progress', 'running'), ('failed', 'blocked'),
    ('idle', 'blocked'), ('review', 'review'), ('completed', 'review')])
def test_truthful_states_never_close_from_worker_completion(setup_intake, kind, expected):
    policy, event, *_ = setup_intake
    event['kind'] = kind
    result = submit(policy, event)
    assert result['status'] == expected
    assert result['goal_status'] == 'unverified'
    if kind == 'failed':
        assert result['external_state'] == 'failed'


def test_explicit_binding_cross_profile_and_closure_refusal(setup_intake):
    policy, event, *_ = setup_intake
    for patch, error in [({'task_id': 'chat'}, 'unbound_task'),
                         ({'owner': 'worker-renamed'}, 'owner_not_allowed'),
                         ({'source_profile': 'stranger'}, 'source_profile_not_allowed'),
                         ({'kind': 'blocked'}, 'user_action_required'),
                         ({'kind': 'accepted'}, 'independent_acceptance_required')]:
        with pytest.raises(RegistryError, match=error):
            intake.prepare(policy, dict(event, **patch))
    with pytest.raises(RegistryError, match='registration_actor_mismatch'):
        intake.check_actor(policy, profile='aiyu-dev', home='/other/profile')


def test_human_pause_priority_and_old_replay_are_preserved(setup_intake):
    policy, event, config, tid = setup_intake
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        kb.edit_task(conn, tid, title='User title', priority=9)
        kb.block_task(conn, tid, reason='User pause')
    assert submit(policy, event)['status'] == 'blocked'
    stale = dict(event, revision=3)
    with pytest.raises(RegistryError, match='registry_refused'):
        submit(policy, stale)
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        task = kb.get_task(conn, tid)
        assert (task.title, task.priority, task.status) == ('User title', 9, 'blocked')


def test_repeated_event_and_projection_failure_never_reexecute_registry(setup_intake):
    policy, event, config, tid = setup_intake
    first = submit(policy, event)
    assert submit(policy, event)['event_sha256'] == first['event_sha256']
    messages, sends = [], []
    def send(packet):
        sends.append(packet)
        if len(sends) == 1:
            return dict(success=False, definitely_not_sent=True, error='offline rejection')
        messages.append(dict(id='message-1', channel_id=packet['thread'],
                             content=packet['content'], author={'id': policy['bot_id']}, mentions=[]))
        return dict(success=True, message_id='message-1')
    packet = first['projection']
    with pytest.raises(RegistryError, match='projection_failed'):
        intake.project(policy, packet, fetch=lambda _: messages, send=send)
    assert intake.project(policy, packet, fetch=lambda _: messages, send=send)['id'] == 'message-1'
    assert intake.project(policy, packet, fetch=lambda _: messages, send=send)['id'] == 'message-1'
    assert len(sends) == 2
    with native.target_scope(config), kbc.connect_closing(board='registry') as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert len(kb.list_comments(conn, tid)) == 3
        assert kb.list_runs(conn, tid) == []


def test_projection_uncertain_author_target_and_mentions_fail_closed(setup_intake):
    policy, event, *_ = setup_intake
    packet = submit(policy, event)['projection']
    for patch in ({'thread': '1569999999999999999'}, {'guild': '1569999999999999999'},
                  {'content': '@everyone'}, {'content': '<@&12345>'}, {'content': '<@1569999999999999999>'}):
        with pytest.raises(RegistryError):
            intake.project(policy, dict(packet, **patch), fetch=lambda _: [], send=lambda _: pytest.fail('sent'))
    def ambiguous(_):
        raise OSError('network timeout')
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.project(policy, packet, fetch=lambda _: [], send=ambiguous)
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.project(policy, packet, fetch=lambda _: [], send=lambda _: pytest.fail('duplicate'))


def test_event_bytes_pinned_and_stale_receipts_not_refreshed(setup_intake):
    policy, event, *_ = setup_intake
    prepared = intake.prepare(policy, event)
    assert hashlib.sha256(prepared['receipt'].read_bytes()).hexdigest() == prepared['event_sha256']
    cfg = json.loads(prepared['config'].read_text())
    assert cfg['evidence']['handoff']['observed_at'] == STAMP
    assert cfg['evidence']['handoff']['claims'] == ['admission:example']
    mutated = copy.deepcopy(event)
    mutated['summary'] = '同revision不同内容'
    submit(policy, event)
    with pytest.raises(RegistryError, match='registry_refused'):
        submit(policy, mutated)


def test_unchanged_observation_no_notification_but_new_transition_does_notify(setup_intake):
    policy, event, *_ = setup_intake
    messages = []
    def send(packet):
        messages.append(dict(id=str(len(messages) + 1), channel_id=packet['thread'],
                             content=packet['content'], author={'id': policy['bot_id']}, mentions=[]))
        return dict(success=True)
    packet = submit(policy, event)['projection']
    intake.project(policy, packet, fetch=lambda _: messages, send=send)
    event = dict(event, revision=6)
    packet = submit(policy, event)['projection']
    intake.project(policy, packet, fetch=lambda _: messages, send=send)
    assert len(messages) == 1
    packet = submit(policy, dict(event, revision=7, kind='failed'))['projection']
    intake.project(policy, packet, fetch=lambda _: messages, send=send)
    packet = submit(policy, dict(event, revision=8, kind='accept'))['projection']
    intake.project(policy, packet, fetch=lambda _: messages, send=send)
    assert len(messages) == 3


def test_explicit_independent_acceptance_only_and_changed_proof_refused(setup_intake, tmp_path):
    policy, event, *_ = setup_intake
    event = dict(event, kind='accepted', source_profile='default')
    proof = tmp_path / 'independent.md'
    proof.write_text('Actual independent acceptance in this isolated fixture.\n')
    policy['acceptance'] = {intake.digest(event): dict(reviewer='default', path=str(proof),
        sha256=hashlib.sha256(proof.read_bytes()).hexdigest())}
    assert submit(policy, event)['status'] == 'done'
    proof.write_text('Changed')
    with pytest.raises(RegistryError, match='acceptance_evidence_changed'):
        intake.prepare(policy, event)


def test_cli_wrong_real_profile_refused_before_output_or_native_call(setup_intake, tmp_path, monkeypatch):
    import hermes_constants
    policy, event, *_ = setup_intake
    cp, ep = tmp_path / 'policy.json', tmp_path / 'event.json'
    cp.write_text(encoded(policy))
    ep.write_text(encoded(event))
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path / 'profiles' / 'aiyu-dev')
    assert intake.main(['--policy', str(cp), '--event', str(ep), 'apply']) == 2
    assert not (tmp_path / 'events').exists()


def test_exact_requester_mention_readback_and_spoofed_author_refused(setup_intake):
    policy, event, *_ = setup_intake
    packet = submit(policy, dict(event, kind='blocked', needs_user=True))['projection']
    messages = []
    def send(p):
        messages.append(dict(id='real-message', channel_id=p['thread'], content=p['content'],
                             author={'id': policy['bot_id']}, mentions=[{'id': policy['requester']}]))
        return dict(success=True)
    assert intake.project(policy, packet, fetch=lambda _: messages, send=send)['id'] == 'real-message'
    messages[0]['author']['id'] = 'stranger'
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.project(policy, packet, fetch=lambda _: messages, send=lambda _: pytest.fail('sent twice'))


def test_shipped_discord_adapter_full_fields_and_no_change_path(setup_intake, monkeypatch):
    from hermes_cli import send_cmd
    from tools import discord_tool
    policy, event, *_ = setup_intake
    binding = policy['bindings'][event['task_id']]
    binding['parent'] = '100000000000000005'
    messages = []
    monkeypatch.setattr(send_cmd, '_load_hermes_env', lambda: None)
    channel = dict(guild_id=policy['guild'], parent_id=binding['parent'])
    monkeypatch.setattr(discord_tool, 'discord_admin_handler', lambda **_: encoded(channel))
    monkeypatch.setattr(discord_tool, 'discord_core', lambda **_: encoded({'messages': messages}))
    monkeypatch.setattr(discord_tool, '_get_bot_token', lambda: 'fixture')
    def get(_method, path, _token):
        if path == '/users/@me':
            return dict(id=policy['bot_id'], bot=True)
        return next(m for m in messages if path.endswith('/' + m['id']))
    monkeypatch.setattr(discord_tool, '_discord_request', get)
    def cli(_policy, args):
        assert args[:3] == ['send', '--to', 'discord:' + binding['thread']]
        messages.append(dict(id=str(len(messages)+1), channel_id=binding['thread'], content=args[3],
                             author={'id': policy['bot_id']}, mentions=[]))
        return dict(success=True)
    monkeypatch.setattr(intake, '_cli', cli)
    first = submit(policy, event)['projection']
    assert intake.discord_projection(policy, first)['id'] == '1'
    second = submit(policy, dict(event, revision=6))['projection']
    assert intake.discord_projection(policy, second)['id'] == '1'
    assert len(messages) == 1
    channel['guild_id'] = 'unauthorized'
    with pytest.raises(RegistryError, match='channel_identity_mismatch'):
        intake.discord_projection(policy, second)


@pytest.mark.parametrize('prior_success', [False, True])
@pytest.mark.parametrize('changed_observation', [False, True])
@pytest.mark.parametrize('lost_response', [False, True])
def test_pending_delivery_survives_revision_and_prior_success(setup_intake, prior_success, changed_observation, lost_response):
    policy, event, *_ = setup_intake
    messages, sent = [], []
    visible, lose_response = True, False
    def send(packet):
        sent.append(packet['event_sha256'])
        messages.append(dict(id=str(len(sent)), channel_id=packet['thread'], content=packet['content'],
                             author={'id': policy['bot_id']}, mentions=[]))
        if lose_response:
            raise OSError('Remote accepted; response lost in isolated fixture')
        return dict(success=True)
    fetch = lambda _: messages if visible else []
    if prior_success:
        prior = submit(policy, dict(event, kind='review', summary='Previously delivered different observation'))['projection']
        intake.project(policy, prior, fetch=fetch, send=send)
    revision = 6 if prior_success else 5
    first = submit(policy, dict(event, revision=revision))['projection']
    visible, lose_response = False, lost_response
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.project(policy, first, fetch=fetch, send=send)
    later_event = dict(event, revision=revision + 1)
    if changed_observation:
        later_event.update(kind='failed', summary='A different substantive observation')
    second = submit(policy, later_event)['projection']
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.project(policy, second, fetch=fetch, send=send)
    assert sent.count(first['event_sha256']) == 1
    assert second['event_sha256'] not in sent
    assert len(sent) == 1 + int(prior_success)
    cursor = json.loads((Path(policy['output']) / 'example.delivery.json').read_text(encoding='utf-8'))
    assert cursor['status'] == 'pending'
    assert cursor['packet'] == first
    visible, lose_response = True, False
    message = intake.project(policy, second, fetch=fetch, send=send)
    expected = second if changed_observation else first
    assert message['content'] == expected['content']
    assert sent.count(first['event_sha256']) == 1
    assert len(sent) == 1 + int(prior_success) + int(changed_observation)


@pytest.mark.parametrize('readback', ['unreadable', 'outside_latest_100'])
def test_pending_delivery_cannot_be_bypassed_by_unavailable_readback(setup_intake, readback):
    policy, event, *_ = setup_intake
    sent = []
    def send(packet):
        sent.append(packet['event_sha256'])
        raise OSError('Remote accepted; response lost')
    first = submit(policy, event)['projection']
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.project(policy, first, fetch=lambda _: [], send=send)
    second = submit(policy, dict(event, revision=6))['projection']
    def fetch(_thread):
        if readback == 'unreadable':
            raise OSError('Read unavailable')
        return [dict(id=str(i), content='unrelated', channel_id=first['thread'],
                     author={'id': policy['bot_id']}, mentions=[]) for i in range(100)]
    with pytest.raises((OSError, RegistryError)):
        intake.project(policy, second, fetch=fetch, send=send)
    assert sent == [first['event_sha256']]


@pytest.mark.parametrize('changed_observation', [False, True])
def test_authoritatively_unsent_retry_keeps_original_marker_across_revisions(setup_intake, changed_observation):
    policy, event, *_ = setup_intake
    messages, sent = [], []
    def send(packet):
        sent.append(packet['event_sha256'])
        if len(sent) == 1:
            return dict(success=False, definitely_not_sent=True)
        messages.append(dict(id='retry', channel_id=packet['thread'], content=packet['content'],
                             author={'id': policy['bot_id']}, mentions=[]))
        return dict(success=True)
    first = submit(policy, event)['projection']
    with pytest.raises(RegistryError, match='projection_failed'):
        intake.project(policy, first, fetch=lambda _: messages, send=send)
    later = dict(event, revision=6)
    if changed_observation:
        later.update(kind='failed', summary='New observation supersedes definitively unsent one')
    second = submit(policy, later)['projection']
    message = intake.project(policy, second, fetch=lambda _: messages, send=send)
    expected = second if changed_observation else first
    assert sent == [first['event_sha256'], expected['event_sha256']]
    assert message['content'] == expected['content']


def test_task_pending_association_is_durable_before_outbound_callback(setup_intake):
    policy, event, *_ = setup_intake
    packet = submit(policy, event)['projection']
    def send(value):
        path = Path(policy['output']) / 'example.delivery.json'
        assert path.is_file(), 'Task pending association must exist before the outbound callback'
        cursor = json.loads(path.read_text(encoding='utf-8'))
        assert cursor['status'] == 'pending'
        assert cursor['packet'] == value == packet
        raise OSError('Response lost after accepting')
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.project(policy, packet, fetch=lambda _: [], send=send)


def test_shipped_adapter_reconciles_pending_original_packet_after_revision_change(setup_intake, monkeypatch):
    from hermes_cli import send_cmd
    from tools import discord_tool
    policy, event, *_ = setup_intake
    binding = policy['bindings'][event['task_id']]
    binding['parent'] = '100000000000000005'
    messages, sent = [], []
    visible = False
    monkeypatch.setattr(send_cmd, '_load_hermes_env', lambda: None)
    monkeypatch.setattr(discord_tool, 'discord_admin_handler', lambda **_: encoded(
        dict(guild_id=policy['guild'], parent_id=binding['parent'])))
    monkeypatch.setattr(discord_tool, 'discord_core', lambda **_: encoded({'messages': messages if visible else []}))
    monkeypatch.setattr(discord_tool, '_get_bot_token', lambda: 'fixture')
    def get(_method, path, _token):
        if path == '/users/@me':
            return dict(id=policy['bot_id'], bot=True)
        return next(m for m in messages if path.endswith('/' + m['id']))
    monkeypatch.setattr(discord_tool, '_discord_request', get)
    def cli(_policy, args):
        sent.append(args[3])
        messages.append(dict(id='accepted', channel_id=binding['thread'], content=args[3],
                             author={'id': policy['bot_id']}, mentions=[]))
        raise OSError('Remote accepted; response lost')
    monkeypatch.setattr(intake, '_cli', cli)
    first = submit(policy, event)['projection']
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.discord_projection(policy, first)
    second = submit(policy, dict(event, revision=6))['projection']
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.discord_projection(policy, second)
    assert sent == [first['content']]
    visible = True
    assert intake.discord_projection(policy, second)['content'] == first['content']
    assert sent == [first['content']]


def test_confirmed_delivery_missing_from_window_does_not_block_new_substantive_change(setup_intake):
    policy, event, *_ = setup_intake
    messages, sent = [], []
    def send(packet):
        sent.append(packet['event_sha256'])
        messages.append(dict(id=str(len(sent)), channel_id=packet['thread'], content=packet['content'],
                             author={'id': policy['bot_id']}, mentions=[]))
        return dict(success=True)
    first = submit(policy, event)['projection']
    intake.project(policy, first, fetch=lambda _: messages, send=send)
    messages.clear()  # Confirmed delivery later falls outside readback, with no new outbound attempt.
    same = submit(policy, dict(event, revision=6))['projection']
    with pytest.raises(RegistryError, match='projection_uncertain'):
        intake.project(policy, same, fetch=lambda _: messages, send=send)
    changed = submit(policy, dict(event, revision=7, kind='failed'))['projection']
    assert intake.project(policy, changed, fetch=lambda _: messages, send=send)['content'] == changed['content']
    assert sent == [first['event_sha256'], changed['event_sha256']]
