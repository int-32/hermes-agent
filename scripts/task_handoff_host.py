"""Read-only live host for the maintained fixed-task handoff checker.

No gateway constructor, session creation, journal writes, dispatch or config writes.
Only a controller-loaded plugin can supply the existing process-owned service.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat

PROTOCOL = 1
MAX_BYTES = 512 * 1024
BINDINGS = ('task_id', 'project', 'profile', 'main_channel', 'thread', 'guild', 'user',
            'message_id', 'home', 'handoff_root', 'document')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':'))


def sha(value):
    return hashlib.sha256(value).hexdigest()


def socket_path(home):
    home = Path(home)
    if not home.is_absolute() or home.resolve(strict=True) != home:
        raise ValueError('canonical existing profile home required')
    path = home / 'runtime' / 'task-handoff.sock'
    if len(str(path).encode()) > 100:
        raise ValueError('profile home exceeds local socket path limit')
    return path


def private_path(path, *, is_socket=False):
    current_uid = getattr(os, 'getuid', None)
    if current_uid is None:
        raise OSError('this local host requires POSIX socket ownership')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PermissionError('symlink refused')
    info = path.stat()
    if info.st_uid != current_uid() or info.st_mode & (0o077 if is_socket else 0o022):
        raise PermissionError('local endpoint ownership or permissions refused')
    if is_socket and not stat.S_ISSOCK(info.st_mode):
        raise PermissionError('not a local socket')
    return info


def query(home, operation, params=None):
    from gateway.control_socket import identify_gateway

    path = socket_path(home)
    private_path(path.parent)
    private_path(path, is_socket=True)
    nonce = secrets.token_hex(16)
    packet = dict(protocol=PROTOCOL, id=nonce, operation=operation, params=params or {})
    raw = (canonical(packet) + '\n').encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('request too large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(90)
        client.connect(str(path))
        client.sendall(raw)
        with client.makefile('rb') as stream:
            reply = stream.readline(MAX_BYTES + 1)
            if len(reply) > MAX_BYTES or not reply.endswith(b'\n'):
                raise ValueError('bounded host response required')
            response = json.loads(reply)
    if (not isinstance(response, dict) or not isinstance(response.get('host_identity'), dict)
            or not isinstance(response.get('result'), dict)):
        raise ValueError('bounded host object required')
    if response.get('id') != nonce or response.get('protocol') != PROTOCOL or response.get('ok') is not True:
        raise PermissionError('fresh host response unavailable')
    identity = identify_gateway(Path(home))
    if not identity or any(response.get('host_identity', {}).get(k) != identity.get(k)
                           for k in ('pid', 'start_time', 'code_sha', 'hermes_home')):
        raise PermissionError('collector is not hosted by the live owning gateway')
    return response['result']


def remote_check(task):
    from scripts.task_handoff_check import Report

    payload = asdict(task)
    payload['native_host'] = False
    try:
        result = query(task.home, 'check', payload)
        if result.pop('task_digest', None) != sha(canonical(payload).encode()):
            raise PermissionError('host response belongs to different inputs')
        if any(result.get(field) != getattr(task, field) for field in ('task_id', 'project', 'profile')):
            raise PermissionError('host task binding mismatch')
        if result.get('required_stage') != task.stage or result.get('status') not in ('PASS', 'FAIL', 'UNVERIFIED'):
            raise PermissionError('host stage mismatch')
        return result
    except (OSError, ValueError, KeyError, TypeError, PermissionError):
        report = Report(task)
        report.add('thread_ready', 'live_host', 'UNVERIFIED', 'local:task-handoff.sock',
                   'fresh authenticated native host unavailable; no downstream execution')
        return report.result()


class Host:
    def __init__(self, service, descriptor):
        self.service, self.descriptor = service, descriptor
        if Path(descriptor['task']['home']) != Path(service.home):
            raise PermissionError('host belongs to a different profile home')
        self.server, self.inode, self.identity = None, None, None
        self.connections, self.closing = set(), False

    async def registration_plan(self):
        raise PermissionError('plan retired; use official kanban source accept')

    async def activate(self, expected_config_sha256):
        raise PermissionError('activate retired; card sources need no policy activation')

    async def check(self, payload):
        from scripts.task_handoff_check import Task, check_task

        if any(str(payload.get(k)) != str(self.descriptor['task'][k]) for k in BINDINGS):
            raise PermissionError('request outside approved fixed task')
        if payload.get('native_host') is not False:
            raise PermissionError('recursive host checks refused')
        if payload.get('native_revision') != self.descriptor['native_revision']:
            raise PermissionError('client pinned a different native source revision')
        body = payload.get('message_body')
        if body is not None and sha(body.encode()) != self.descriptor['scope_body_sha256']:
            raise PermissionError('scope body differs from independently selected message')
        values = dict(payload)
        for key in ('home', 'handoff_root', 'document'):
            values[key] = Path(values[key])
        task = Task(**values)
        result = await check_task(task)
        result['task_digest'] = sha(canonical(payload).encode())
        result['host'] = dict(pid=os.getpid(), protocol=PROTOCOL, code_sha=self.identity['code_sha'],
                              descriptor_sha256=sha(canonical(self.descriptor).encode()))
        return result

    async def dispatch(self, request):
        if request.get('protocol') != PROTOCOL:
            raise ValueError('unsupported host protocol')
        operation, params = request.get('operation'), request.get('params') or {}
        handlers = {'status': self.status, 'check': lambda: self.check(params)}
        if operation not in handlers or not self.service.runner._running or self.closing:
            raise PermissionError('unsupported operation or inactive native service')
        return await handlers[operation]()

    async def status(self):
        return dict(state='native_host_attached', pid=os.getpid(), task_id=self.descriptor['task']['task_id'],
                    operations=['status', 'check'], retired_operations=['plan', 'activate'], receipt_verified=False,
                    native_revision=self.descriptor['native_revision'],
                    descriptor_sha256=sha(canonical(self.descriptor).encode()))

    async def connection(self, reader, writer):
        request, response = {}, dict(ok=False, error='unavailable')
        current = asyncio.current_task()
        self.connections.add(current)
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if not raw.endswith(b'\n') or len(raw) > MAX_BYTES:
                raise ValueError('bounded JSON line required')
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError('request must be an object')
            request = parsed
            result = await asyncio.wait_for(self.dispatch(request), timeout=80)
            response = dict(ok=True, result=result)
        except Exception as exc:  # health: allow BLE001 -- redact native/transport errors, never send error bodies
            response = dict(ok=False, error=type(exc).__name__)
        finally:
            # No source/error bodies or credentials cross the local transport.
            response.update(id=request.get('id'), protocol=PROTOCOL, host_identity=self.identity)
            raw = (canonical(response) + '\n').encode()
            if len(raw) <= MAX_BYTES:
                writer.write(raw)
                await writer.drain()
            writer.close()
            await writer.wait_closed()
            self.connections.discard(current)

    async def serve(self):
        from gateway.control_socket import identify_gateway

        self.identity = await asyncio.to_thread(identify_gateway, Path(self.service.home))
        if (not self.identity or self.identity.get('pid') != os.getpid()
                or self.identity.get('code_sha') != self.descriptor['native_revision']):
            raise PermissionError('host is not the reviewed live gateway')
        path = socket_path(self.service.home)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        private_path(path.parent)
        # Native force reload cancels supervised tasks asynchronously. Allow the
        # old host to remove its own inode; never replace or unlink its endpoint.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while path.exists() and not path.is_symlink() and loop.time() < deadline:
            await asyncio.sleep(.05)
        if path.exists() or path.is_symlink():
            raise PermissionError('existing socket is never replaced')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            old_umask = os.umask(0o177)
            try:
                listener.bind(str(path))
            finally:
                # No await while the process-wide umask is changed.
                os.umask(old_umask)
            self.inode = path.stat().st_ino
            listener.setblocking(False)
            try:
                self.server = await asyncio.start_unix_server(self.connection, sock=listener, limit=MAX_BYTES)
                async with self.server:
                    await self.server.serve_forever()
            finally:
                self.closing = True
                for task in list(self.connections):
                    task.cancel()
                await asyncio.gather(*self.connections, return_exceptions=True)
                if path.exists() and path.stat().st_ino == self.inode:
                    path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('operation', choices=('status', 'plan', 'activate'))
    parser.add_argument('--expected-config-sha256')
    args = parser.parse_args(argv)
    if args.operation in {'plan', 'activate'}:
        print(canonical(dict(state='retired', reason='use official kanban source accept; no tasks config activation')))
        return 2
    try:
        result = query(args.home, args.operation, dict(expected_config_sha256=args.expected_config_sha256))
        code = 0
    except (OSError, ValueError, KeyError, PermissionError):
        result, code = dict(state='UNVERIFIED', reason='reviewed native host unavailable or refused'), 3
    print(canonical(result))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
