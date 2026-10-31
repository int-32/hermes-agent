#!/usr/bin/env python3
"""Read-only task handoff verification and an opt-in, fail-closed argv guard.

No daemon, identity creation, gateway writes, model calls or automatic dispatch.
See task_handoff_check.md for the in-process native collector boundary.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

# Both `python scripts/task_handoff_check.py` and `from scripts import ...`.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import task_handoff_native as native

STAGES = ('thread_ready', 'handoff_received', 'execution_started', 'result_routed')
EXIT = {'PASS': 0, 'FAIL': 2, 'UNVERIFIED': 3}


@dataclass
class Task:
    task_id: str
    project: str
    profile: str
    home: Path
    handoff_root: Path
    document: Path
    main_channel: str
    thread: str
    guild: str
    user: str
    message_id: str
    message_body: str | None
    request_id: str | None
    stage: str
    max_age: float = 600
    local_only: bool = False
    exec_timeout: float = 3600
    native_host: bool = False
    native_revision: str | None = None


def document_pointer(task):
    """Match native document_pointer's canonical, bounded no-follow contract."""
    if not all(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', s) for s in (task.task_id, task.project)):
        raise ValueError('invalid task/project identifier')
    root, path = task.handoff_root, task.document
    expected = root / task.project / task.task_id / 'HANDOFF.md'
    if (not root.is_absolute() or root.resolve(strict=True) != root or path != expected
            or path.resolve(strict=True) != path or '..' in path.parts):
        raise ValueError('document must be canonical <root>/<project>/<task>/HANDOFF.md')
    # Reuse the native directory-fd reader when present; standalone has the same bounded walk.
    import os
    import stat
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (task.project, task.task_id):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open('HANDOFF.md', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                raise ValueError('document must be a regular file <= 256 KiB')
            body = stream.read(262145)
            if len(body) > 262144:
                raise ValueError('document too large')
    finally:
        os.close(directory)
    return dict(task_id=task.task_id, project=task.project, path=str(path), sha256=hashlib.sha256(body).hexdigest())


def validate_task(task):
    if task.stage not in STAGES:
        raise ValueError('unknown required stage')
    if (not math.isfinite(task.max_age) or not 0 < task.max_age <= 86400
            or not math.isfinite(task.exec_timeout) or not 0 < task.exec_timeout <= 86400):
        raise ValueError('age/timeout must be finite, positive and <= 86400 seconds')
    if not task.home.is_absolute() or task.home.resolve(strict=True) != task.home:
        raise ValueError('profile home must be canonical')
    for identifier in (task.main_channel, task.thread, task.guild, task.user, task.message_id):
        if not re.fullmatch(r'[0-9]{1,25}', identifier):
            raise ValueError('Discord identifiers must be decimal IDs')
    if task.request_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', task.request_id):
        raise ValueError('invalid native request ID')


class Report:
    def __init__(self, task):
        self.task, self.checks = task, []

    def add(self, stage, name, status, source, reason):
        self.checks.append(dict(stage=stage, check=name, status=status, source=source, reason=reason))

    def expect(self, stage, name, pairs, source):
        """A missing observation is unknown; a contradictory observation is failure."""
        status = 'PASS'
        for observed, expected in pairs:
            if observed is None:
                status = 'UNVERIFIED' if status != 'FAIL' else status
            elif type(observed) is not type(expected) or observed != expected:
                status = 'FAIL'
        self.add(stage, name, status, source,
                 {'PASS': 'independently matched', 'FAIL': 'observation contradicts required binding',
                  'UNVERIFIED': 'required observation unavailable'}[status])
        return status == 'PASS'

    def result(self):
        stages = {}
        for index, stage in enumerate(STAGES):
            statuses = [row['status'] for row in self.checks if row['stage'] in STAGES[:index + 1]]
            stages[stage] = aggregate(statuses)
        return dict(schema=1, task_id=self.task.task_id, project=self.task.project, profile=self.task.profile,
                    required_stage=self.task.stage, evidence_mode='local-only' if self.task.local_only else 'direct-read',
                    status=stages[self.task.stage], stages=stages, checks=self.checks,
                    coverage='explicit check/exec calls only; no global interception or semantic command classification')


def aggregate(statuses):
    if 'FAIL' in statuses:
        return 'FAIL'
    return 'UNVERIFIED' if not statuses or 'UNVERIFIED' in statuses else 'PASS'


def discord_get(task, path):
    """GET only. Secrets stay in the official profile scope; no environment fallback."""
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
    from tools.discord_tool import _get_bot_token, _discord_request
    scope = build_profile_secret_scope(task.home)
    # The official getter may fall back to ambient env on a scoped miss in single-profile mode.
    # Require a token in this explicitly selected profile's mapping first.
    if not scope.get('DISCORD_BOT_TOKEN', '').strip():
        raise PermissionError('selected profile has no scoped bot token')
    token_scope = set_secret_scope(scope, profile_home=str(task.home))
    try:
        token = _get_bot_token()
        if not token:
            raise PermissionError('no scoped bot token')
        return _discord_request('GET', path, token, timeout=10)
    finally:
        reset_secret_scope(token_scope)


def read_discord(task, report, stage, name, path):
    source = 'discord:GET ' + path
    if task.local_only:
        report.add(stage, name, 'UNVERIFIED', source, 'platform readback deliberately not performed')
        return None
    try:
        result = discord_get(task, path)
        if not isinstance(result, dict):
            raise ValueError('unexpected platform shape')
        return result
    except Exception as exc:  # health: allow BLE001 -- redact credential-bearing platform error bodies
        # Never serialize exception bodies: they may contain API bodies, URLs or secrets.
        report.add(stage, name, 'UNVERIFIED', source, 'readback unavailable: ' + type(exc).__name__)
        return None


def thread_checks(task, report):
    stage = 'thread_ready'
    channel = read_discord(task, report, stage, 'thread', f'/channels/{task.thread}')
    if channel is not None:
        report.expect(stage, 'thread_binding', [(channel.get('id'), task.thread),
                      (channel.get('parent_id'), task.main_channel), (channel.get('guild_id'), task.guild)],
                      f'discord:GET /channels/{task.thread}')
        kind = channel.get('type')
        report.expect(stage, 'thread_type', [(kind in (10, 11, 12) if kind is not None else None, True)],
                      f'discord:GET /channels/{task.thread}')
        metadata = channel.get('thread_metadata') or {}
        report.expect(stage, 'active_thread', [(metadata.get('archived'), False), (metadata.get('locked'), False)],
                      f'discord:GET /channels/{task.thread}#thread_metadata')
    membership_path = f'/channels/{task.thread}/thread-members/{task.user}'
    member = read_discord(task, report, stage, 'membership', membership_path)
    if member is not None:
        report.expect(stage, 'membership_binding', [(member.get('id'), task.thread), (member.get('user_id'), task.user)],
                      'discord:GET ' + membership_path)
    message_path = f'/channels/{task.thread}/messages/{task.message_id}'
    message = read_discord(task, report, stage, 'scope_message', message_path)
    if message is not None:
        report.expect(stage, 'scope_message_binding', [(message.get('id'), task.message_id),
                      (message.get('channel_id'), task.thread)], 'discord:GET ' + message_path)
        if task.message_body is not None:
            report.expect(stage, 'scope_body', [(message.get('content'), task.message_body)],
                          'discord:GET ' + message_path + '#content(exact; body not emitted)')
        if not (message.get('content') or '').strip():
            report.add(stage, 'empty_scope', 'FAIL', 'discord:GET ' + message_path, 'thread has no readable scope body')


async def check_task(task):
    validate_task(task)
    if task.native_host:
        from scripts.task_handoff_host import remote_check
        return await asyncio.to_thread(remote_check, task)
    report = Report(task)
    try:
        pointer = await asyncio.to_thread(document_pointer, task)
        report.add('thread_ready', 'document', 'PASS', str(task.document) + '#sha256=' + pointer['sha256'],
                   'canonical bounded native document pointer')
    except (OSError, ValueError) as exc:
        report.add('thread_ready', 'document', 'FAIL' if isinstance(exc, ValueError) else 'UNVERIFIED',
                   str(task.document), 'document unavailable or unsafe: ' + type(exc).__name__)
        pointer = None
    if task.message_body is None:
        report.add('thread_ready', 'expected_scope_body', 'UNVERIFIED', 'input:message-body', 'no independently selected expected scope body')
    elif not task.message_body.strip() or re.fullmatch(r'https?://\S+', task.message_body.strip()):
        report.add('thread_ready', 'substantive_scope', 'FAIL', 'input:message-body', 'empty or link-only scope')
    await asyncio.to_thread(thread_checks, task, report)
    await native.collect(task, pointer, report, read_discord)
    return report.result()


async def guard(task, command):
    if not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError('exec requires a nonempty argv array')
    result = await check_task(task)
    if result['status'] != 'PASS':
        result['exec'] = dict(started=False)
        return result, EXIT[result['status']]
    # No saved report/boolean is accepted here; every invocation collects again.
    try:
        completed = await asyncio.to_thread(subprocess.run, command, shell=False, stdout=sys.stderr,
                                            stderr=sys.stderr, check=False, timeout=task.exec_timeout)
    except subprocess.TimeoutExpired:
        result['exec'] = dict(started=True, timed_out=True, returncode=124)
        return result, 124
    code = completed.returncode if completed.returncode >= 0 else 128 - completed.returncode
    result['exec'] = dict(started=True, timed_out=False, returncode=code)
    return result, code


class InputParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('invalid CLI arguments')


def parser():
    result = InputParser(description=__doc__)
    result.add_argument('action', choices=('check', 'exec'))
    for name in ('task-id', 'project', 'profile', 'profile-home', 'handoff-root', 'document',
                 'main-channel', 'thread', 'guild', 'user', 'message-id',
                 'hermes-source', 'source-revision', 'evidence-source'):
        result.add_argument('--' + name, required=True)
    result.add_argument('--message-body-file', help='exact expected UTF-8 scope body; absent means UNVERIFIED')
    result.add_argument('--request-id')
    result.add_argument('--exec-timeout', type=float, default=3600)
    result.add_argument('--stage', choices=STAGES, required=True)
    result.add_argument('--max-age', type=float, default=600)
    result.add_argument('--local-only', action='store_true', help='no network; thread readiness remains UNVERIFIED')
    result.add_argument('--native-host', action='store_true', help='fresh check via the reviewed local gateway plugin; no saved PASS')
    return result


def select_source(args):
    source = Path(args.hermes_source)
    if not source.is_absolute() or source.resolve(strict=True) != source:
        raise ValueError('Hermes source must be an explicit canonical absolute path')
    if not re.fullmatch(r'[0-9a-f]{40}', args.source_revision):
        raise ValueError('source-revision must be a full Git SHA')
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()
    if revision != args.source_revision or args.evidence_source != 'native':
        raise ValueError('pinned source revision / native evidence source required')
    dirty = subprocess.run(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'],
                          capture_output=True, text=True, check=True, timeout=10).stdout
    if dirty:
        raise ValueError('native source has tracked changes; freeze and review it first')
    for name in ('gateway', 'agent', 'tools', 'hermes_state'):
        module = sys.modules.get(name)
        if module is not None and not Path(module.__file__).resolve().is_relative_to(source):
            raise ValueError('already imported Hermes modules belong to a different source')
    sys.path.insert(0, str(source))
    return revision


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index('--') if '--' in argv else len(argv)
    command = argv[split + 1:] if split < len(argv) else []
    try:
        args = parser().parse_args(argv[:split])
        revision = select_source(args)
        if (not math.isfinite(args.max_age) or not 0 < args.max_age <= 86400
                or args.action == 'exec' and not command or args.action == 'check' and command):
            raise ValueError('invalid age or command (exec requires -- argv)')
        for name in ('main_channel', 'thread', 'guild', 'user', 'message_id'):
            if not re.fullmatch(r'[0-9]{1,25}', getattr(args, name)):
                raise ValueError('Discord identifiers must be decimal IDs')
        task = Task(args.task_id, args.project, args.profile, Path(args.profile_home), Path(args.handoff_root),
                    Path(args.document), args.main_channel, args.thread, args.guild, args.user, args.message_id,
                    Path(args.message_body_file).read_text(encoding='utf-8-sig') if args.message_body_file else None, args.request_id, args.stage,
                    args.max_age, args.local_only, args.exec_timeout, args.native_host, revision)
        if not task.home.is_absolute() or task.home.resolve(strict=True) != task.home:
            raise ValueError('profile home must be canonical and absolute')
        if args.action == 'exec':
            result, code = asyncio.run(guard(task, command))
        else:
            result = asyncio.run(check_task(task))
            code = EXIT[result['status']]
        result['source_revision'] = revision
    except Exception as exc:  # health: allow BLE001 -- CLI failures must emit redacted nonzero JSON, never credentials
        result = dict(status='UNVERIFIED', checks=[dict(check='inputs_or_collector', status='UNVERIFIED',
                      source='CLI/direct collector', reason='cannot verify: ' + type(exc).__name__)])
        code = 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
