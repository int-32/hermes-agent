"""Explicit agent acceptance/status → existing native registry; Discord is a projection.

Run in the controller profile. Producer observations are reviewed inputs, never
credentials or native handoff receipts. This command neither runs agents nor HWB.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path

from scripts.kanban_registry_records import (
    RegistryError, checked_path, digest, encoded, identifier, read_json, text, timestamp,
)

STATES = {'accept': 'running', 'progress': 'running', 'failed': 'failed', 'idle': 'idle',
          'blocked': 'waiting_user', 'review': 'review', 'completed': 'completed', 'accepted': 'completed'}


def check_actor(policy, *, profile, home):
    if profile != policy['actor_profile'] or Path(home).resolve() != Path(policy['actor_home']).resolve():
        raise RegistryError('registration_actor_mismatch')


def validate_event(policy, event):
    binding = policy['bindings'].get(event['task_id'])
    if not binding:
        raise RegistryError('unbound_task')
    if event['owner'] not in binding['owners']:
        raise RegistryError('owner_not_allowed')
    if event['source_profile'] not in binding['source_profiles']:
        raise RegistryError('source_profile_not_allowed')
    for field in ('owner', 'source_profile', 'source_session_id', 'task_id'):
        identifier(event[field])
    text(event['summary'], limit=500)
    timestamp(event['observed_at'])
    if type(event['revision']) is not int or event['revision'] < 1 or event['kind'] not in STATES:
        raise RegistryError('invalid_event')
    if type(event['needs_user']) is not bool:
        raise RegistryError('invalid_mention_intent')
    if event['kind'] == 'blocked' and not event['needs_user']:
        raise RegistryError('user_action_required')
    if event['kind'] == 'accepted':
        # Acceptance evidence is separately reviewed operator policy, not a producer's boolean.
        approved = policy.get('acceptance', {}).get(digest(event))
        if not approved or event['source_profile'] == event['owner'] or approved['reviewer'] != policy['actor_profile']:
            raise RegistryError('independent_acceptance_required')
        proof = checked_path(Path(approved['path']))
        if hashlib.sha256(proof.read_bytes()).hexdigest() != approved['sha256']:
            raise RegistryError('acceptance_evidence_changed')
    return binding


def _write_once(path, value):
    data = (encoded(value) + '\n').encode('utf-8')
    if path.exists():
        if path.read_bytes() != data:
            raise RegistryError('prepared_input_changed')
    else:
        path.write_bytes(data)
    return path


def prepare(policy, event):
    if read_json(Path(policy['registry_config'])).get('mode') == 'native':
        raise RegistryError('legacy_intake_retired_use_kanban_source')
    binding = validate_event(policy, event)
    root = checked_path(Path(policy['output']))
    raw = (encoded(event) + '\n').encode('utf-8')
    sha = hashlib.sha256(raw).hexdigest()
    folder = checked_path(root / sha, root=root)
    folder.mkdir(parents=True, exist_ok=True)
    baseline = read_json(Path(policy['records']))
    matches = [r for r in baseline if r['task_id'] == event['task_id']]
    if len(matches) != 1:
        raise RegistryError('one_explicit_task_required')
    record = copy.deepcopy(matches[0])
    if record['thread'] != binding['thread'] or record['project'] != binding['project']:
        raise RegistryError('task_binding_changed')
    config = copy.deepcopy(read_json(Path(policy['registry_config'])))
    if record['profile'] != policy['actor_profile'] or config['profile'] != policy['actor_profile']:
        raise RegistryError('registration_namespace_mismatch')
    if config.get('mode') == 'native' and any(
        Path(config[k]).resolve() != Path(policy['actor_home']).resolve()
        for k in ('profile_home', 'kanban_home', 'dispatcher_home')
    ):
        raise RegistryError('canonical_controller_home_required')
    admission = config['evidence'][record['handoff']]
    admission_path = checked_path(Path(config['source_root']) / admission['path'])
    admission_bytes = admission_path.read_bytes()
    if hashlib.sha256(admission_bytes).hexdigest() != admission['sha256']:
        raise RegistryError('admission_evidence_changed')
    # Preserve original admission bytes/time/identity; discard stale external claims from this input.
    target = folder / 'admission.md'
    if target.exists() and target.read_bytes() != admission_bytes:
        raise RegistryError('admission_copy_changed')
    target.write_bytes(admission_bytes)
    config['source_root'] = str(folder)
    config['evidence'] = {record['handoff']: dict(admission, path='admission.md',
                           claims=['admission:' + record['task_id']])}
    ref = 'agent-event-' + sha
    claims = ['external:' + STATES[event['kind']]]
    accepted = event['kind'] == 'accepted'
    if accepted:
        claims.append('goal:accepted')
    config['evidence'][ref] = dict(path='event.json', sha256=sha, profile=record['profile'],
        task_id=record['task_id'], observed_at=event['observed_at'], claims=claims)
    record.update(revision=event['revision'], observed_at=event['observed_at'], receipts=[ref],
                  external_state=STATES[event['kind']], goal_status='accepted' if accepted else 'unverified',
                  stages=dict.fromkeys(('development', 'merge', 'deployment'), 'unknown'))
    record['executor_ids'] = sorted(set(record['executor_ids'] + event['executor_ids'] + [
        'owner:' + event['owner'], 'source:' + event['source_profile'] + ':' + event['source_session_id'],
        'registration-actor:' + policy['actor_profile']]))
    return dict(event_sha256=sha, event=event, binding=binding, original_owner=record['owner'],
                receipt=_write_once(folder / 'event.json', event),
                config=_write_once(folder / 'registry-policy.json', config),
                records=_write_once(folder / 'records.json', [record]))


def _command(policy, args):
    done = subprocess.run([policy['cli'], *args], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, check=False)
    path = checked_path(Path(policy['output']) / 'command-receipts.jsonl')
    with path.open('a', encoding='utf-8') as receipt:
        receipt.write(encoded(dict(argv=[policy['cli'], *args], exit=done.returncode,
                                   stdout=done.stdout, stderr=done.stderr)) + '\n')
    return done


def _cli(policy, args):
    done = _command(policy, args)
    try:
        value = json.loads(done.stdout)
    except ValueError:
        raise RegistryError('official_cli_unavailable') from None
    if done.returncode not in (0, 3):
        raise RegistryError('official_cli_refused')
    return value


def annotate_native(policy, native_id, body):
    task = _cli(policy, ['kanban', '--board', 'registry', 'show', native_id, '--json'])
    if any(c['body'] == body and c['author'] == policy['actor_profile'] for c in task['comments']):
        return
    done = _command(policy, ['kanban', '--board', 'registry', 'comment', native_id,
                            body, '--author', policy['actor_profile']])
    if done.returncode:
        raise RegistryError('custody_annotation_failed')


def apply_event(policy, prepared, *, registry=None, annotate=annotate_native):
    from hermes_cli.active_sessions import _FileLock
    path = checked_path(Path(policy['output']) / (prepared['event']['task_id'] + '.lock'))
    with _FileLock(path):
        return _apply_locked(policy, prepared, registry, annotate)


def _apply_locked(policy, prepared, registry, annotate):
    if registry is None:
        registry = lambda cp, rp, cmd: _cli(policy, ['kanban', 'registry', '--config', str(cp), '--records', str(rp), cmd])
    cp, rp = prepared['config'], prepared['records']
    before = registry(cp, rp, 'plan')
    row = before['rows'][0]
    expected = prepared['binding'].get('native_id')
    if row['action'].startswith('blocked_') or expected and row['native_id'] != expected:
        raise RegistryError('registry_refused')
    after = registry(cp, rp, 'sync')
    row = after['rows'][0]
    if row['action'].startswith('blocked_') or after.get('write_result') == 'blocked_batch':
        raise RegistryError('registry_refused')
    event = prepared['event']
    custody = dict(schema='agent-task-intake/v1', event_sha256=prepared['event_sha256'],
        task_id=event['task_id'], current_owner=event['owner'], original_owner=prepared['original_owner'],
        registration_actor=policy['actor_profile'], source_profile=event['source_profile'],
        source_session_id=event['source_session_id'], source_identity='controller-reviewed observation; not a native handoff receipt')
    annotate(policy, row['native_id'], encoded(custody))
    packet = dict(task_id=event['task_id'], guild=policy['guild'], thread=prepared['binding']['thread'],
                  event_sha256=prepared['event_sha256'], needs_user=event['needs_user'])
    marker = '[agent-event:' + prepared['event_sha256'] + ']'
    mention = '<@' + policy['requester'] + '> ' if event['needs_user'] else ''
    packet['content'] = (f"{mention}{event['task_id']} | {event['kind']} | owner={event['owner']} | "
                         f"board={row['native_status']}\n{event['summary']}\n{marker}")
    packet['fingerprint'] = digest([event['task_id'], event['kind'], event['owner'],
                                    row['native_status'], event['summary'], event['needs_user']])
    _write_once(prepared['receipt'].parent / 'projection.json', packet)
    return dict(custody, native_id=row['native_id'], status=row['native_status'],
                external_state=row['observation']['external_state'], goal_status=row['observation']['goal_status'],
                projection=packet, changed=row['observation'] != before['rows'][0]['observation'] or before['rows'][0]['action'] != 'noop')


def validate_packet(policy, packet):
    if not re.fullmatch('[a-f0-9]{64}', packet['event_sha256']):
        raise RegistryError('invalid_projection_digest')
    binding = policy['bindings'].get(packet['task_id'])
    if not binding or packet['guild'] != policy['guild'] or packet['thread'] != binding['thread']:
        raise RegistryError('unauthorized_destination')
    body = packet['content']
    mentions = re.findall(r'<@!?([0-9]+)>', body)
    expected = [policy['requester']] if packet['needs_user'] else []
    if mentions != expected or '@everyone' in body or '@here' in body or '<@&' in body:
        raise RegistryError('unauthorized_mention')
    if len(body) > 1800 or '[agent-event:' + packet['event_sha256'] + ']' not in body:
        raise RegistryError('invalid_projection')
    path = checked_path(Path(policy['output']) / packet['event_sha256'] / 'projection.json')
    if read_json(path) != packet:
        raise RegistryError('projection_bytes_changed')


def project(policy, packet, *, fetch, send):
    from hermes_cli.active_sessions import _FileLock
    validate_packet(policy, packet)
    path = checked_path(Path(policy['output']) / packet['event_sha256'] / 'delivery.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FileLock(Path(policy['output']) / (packet['task_id'] + '.delivery.lock')):
        cursor = Path(policy['output']) / (packet['task_id'] + '.delivery.json')
        last = read_json(cursor) if cursor.exists() else {}
        if last.get('status') == 'pending':
            # Reconcile the original marker before any later observation can send.
            message = _project_packet(policy, last['packet'], cursor, fetch, send)
            if last['packet']['fingerprint'] == packet['fingerprint']:
                return message
        elif last.get('packet', {}).get('fingerprint') == packet['fingerprint']:
            packet = last['packet']
        return _project_packet(policy, packet, cursor, fetch, send)


def _project_packet(policy, packet, cursor, fetch, send):
    validate_packet(policy, packet)
    path = checked_path(Path(policy['output']) / packet['event_sha256'] / 'delivery.json')

    def tracked_send(value):
        # A lost response must not leave only an older successful task cursor.
        cursor.write_text(encoded(dict(status='pending', packet=value)) + '\n', encoding='utf-8')
        result = send(value)
        if not result.get('success') and result.get('definitely_not_sent') is True:
            cursor.write_text(encoded(dict(status='failed', packet=value)) + '\n', encoding='utf-8')
        return result

    message = _project_locked(policy, packet, path, fetch, tracked_send)
    cursor.write_text(encoded(dict(status='delivered', packet=packet, message_id=message['id'])) + '\n', encoding='utf-8')
    return message


def _project_locked(policy, packet, path, fetch, send):
    previous = read_json(path) if path.exists() else {}
    messages = fetch(packet['thread'])  # Failure to reconcile is never permission to resend.
    matches = [m for m in messages if m['content'] == packet['content'] and
               m.get('channel_id') == packet['thread'] and m['author']['id'] == policy['bot_id']]
    if matches:
        message = matches[0]
        expected = [policy['requester']] if packet['needs_user'] else []
        if [m['id'] for m in message.get('mentions', [])] != expected:
            raise RegistryError('mention_readback_mismatch')
        path.write_text(encoded(dict(status='delivered', message=message)) + '\n', encoding='utf-8')
        return message
    if previous.get('status') in {'sending', 'uncertain', 'delivered'}:
        raise RegistryError('projection_uncertain')
    path.write_text(encoded(dict(status='sending')) + '\n', encoding='utf-8')
    try:
        result = send(packet)
    except (OSError, RegistryError, subprocess.SubprocessError):
        path.write_text(encoded(dict(status='uncertain')) + '\n', encoding='utf-8')
        raise RegistryError('projection_uncertain') from None
    if not result.get('success'):
        status = 'failed' if result.get('definitely_not_sent') is True else 'uncertain'
        path.write_text(encoded(dict(status=status, result=result)) + '\n', encoding='utf-8')
        raise RegistryError('projection_failed' if status == 'failed' else 'projection_uncertain')
    # Reconcile the exact body, author, channel and mention fields after successful send.
    path.write_text(encoded(dict(status='sending', result=result)) + '\n', encoding='utf-8')
    return _project_locked(policy, packet, path, fetch, send)


def discord_projection(policy, packet):
    """Explicit authorized send using shipped tools; full native GET supplies mention readback."""
    from hermes_cli.send_cmd import _load_hermes_env
    from tools import discord_tool as discord
    validate_packet(policy, packet)
    _load_hermes_env()
    channel = json.loads(discord.discord_admin_handler(action='channel_info', channel_id=packet['thread']))
    binding = policy['bindings'][packet['task_id']]
    if channel.get('guild_id') != packet['guild'] or channel.get('parent_id') != binding['parent']:
        raise RegistryError('channel_identity_mismatch')
    me = discord._discord_request('GET', '/users/@me', discord._get_bot_token())
    if me.get('id') != policy['bot_id'] or me.get('bot') is not True:
        raise RegistryError('bot_identity_mismatch')
    contents = {packet['content']}
    cursor = Path(policy['output']) / (packet['task_id'] + '.delivery.json')
    if cursor.exists():
        contents.add(read_json(cursor)['packet']['content'])

    def fetch(thread):
        result = json.loads(discord.discord_core(action='fetch_messages', channel_id=thread, limit=100))
        if 'messages' not in result:
            raise RegistryError('discord_readback_unavailable')
        # The public summary omits channel_id/mentions. Reuse its native REST helper only
        # for exact matching messages after the public action's permission/allowlist gate.
        return [discord._discord_request('GET', f"/channels/{thread}/messages/{m['id']}", discord._get_bot_token())
                for m in result['messages'] if m['content'] in contents and m['author']['id'] == policy['bot_id']]

    def send(value):
        result = _cli(policy, ['send', '--to', 'discord:' + value['thread'], value['content'], '--json'])
        # Unknown failures remain ambiguous; never infer unsent from a timeout string.
        return dict(result, definitely_not_sent=False)

    return project(policy, packet, fetch=fetch, send=send)


def main(argv=None):
    import sys
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in and args_in[0] in {'accept', 'observe', 'show'}:
        from hermes_cli.kanban_source import main as source_main
        return source_main(args_in)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--event', type=Path)
    parser.add_argument('--projection', type=Path)
    parser.add_argument('command', choices=('prepare', 'apply', 'project'))
    args = parser.parse_args(args_in)
    try:
        policy = read_json(args.policy)
        if args.command != 'prepare':
            from hermes_constants import get_hermes_home, profile_name_for_home
            home = get_hermes_home()
            check_actor(policy, profile=profile_name_for_home(home), home=home)
        if args.command == 'project':
            print(encoded(discord_projection(policy, read_json(args.projection))))
            return 0
        event = read_json(args.event)
        prepared = prepare(policy, event)
        result = apply_event(policy, prepared) if args.command == 'apply' else {
            k: str(v) for k, v in prepared.items() if isinstance(v, (str, Path))}
        print(encoded(result))
        return 0
    except (RegistryError, KeyError, ValueError, OSError) as exc:
        print(encoded(dict(error=str(exc) if isinstance(exc, RegistryError) else 'invalid_or_unavailable_input')))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
