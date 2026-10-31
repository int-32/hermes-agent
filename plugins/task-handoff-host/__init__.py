"""Opt-in host; loaded through existing plugin reload, never constructs a gateway."""

import hashlib
import importlib
import json
import logging
from pathlib import Path
import sys

logger = logging.getLogger(__name__)


def verified_modules(path):
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise PermissionError('reviewed canonical descriptor required')
    descriptor = json.loads(path.read_text(encoding='utf-8-sig'))
    native_root = Path(descriptor['native_source'])
    if not native_root.is_absolute() or native_root.resolve(strict=True) != native_root:
        raise PermissionError('reviewed canonical native source required')
    for relative, expected in descriptor['native_files'].items():
        file = native_root / relative
        if not file.is_relative_to(native_root) or '..' in file.parts or file.resolve(strict=True) != file:
            raise PermissionError('native source path escaped reviewed root')
        if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise PermissionError('native source bytes differ from reviewed baseline')
        loaded = sys.modules.get(relative[:-3].replace('/', '.'))
        if loaded is not None and Path(loaded.__file__).resolve() != file:
            raise PermissionError('already loaded native module belongs to another source')
    root = Path(descriptor['bundle_root'])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise PermissionError('canonical bundle required')
    allowed = {'scripts/task_handoff_check.py', 'scripts/task_handoff_native.py', 'scripts/task_handoff_host.py'}
    if set(descriptor['bundle_files']) != allowed:
        raise PermissionError('exact reviewed host bundle required')
    for relative, expected in descriptor['bundle_files'].items():
        file = root / relative
        if file.resolve(strict=True) != file or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise PermissionError('reviewed bundle bytes changed')
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    modules = {}
    for relative, expected in descriptor['bundle_files'].items():
        name = relative[:-3].replace('/', '.')
        previous = sys.modules.get(name)
        if previous is not None and getattr(previous, '_HANDOFF_HOST_PIN', None) != expected:
            raise PermissionError('already loaded checker is not this reviewed bundle')
    for relative, expected in descriptor['bundle_files'].items():
        name = relative[:-3].replace('/', '.')
        module = importlib.import_module(name)
        if Path(module.__file__).resolve() != root / relative:
            raise PermissionError('checker import resolved outside reviewed bundle')
        module._HANDOFF_HOST_PIN = expected
        modules[name] = module
    return descriptor, modules['scripts.task_handoff_host']


def register(ctx):
    from hermes_constants import get_hermes_home

    descriptor, module = verified_modules(Path(ctx.get_config('descriptor')))
    if Path(descriptor['task']['home']) != Path(get_hermes_home()).resolve():
        raise PermissionError('plugin loaded under another profile')
    state = dict(stopped=False, scheduled=False, task=None, loop=None)

    def cancel():
        state['stopped'] = True
        if state['loop'] and state['task']:
            state['loop'].call_soon_threadsafe(state['task'].cancel)

    def attach(**unused):
        handoff = sys.modules.get('gateway.agent_handoff')
        service = getattr(handoff, '_service', None)
        if state['stopped'] or state['scheduled'] or service is None or not service.runner._running:
            return
        loop = service.runner._gateway_loop
        state.update(scheduled=True, loop=loop)

        def start():
            if state['stopped']:
                return
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
            token = set_hermes_home_override(service.home)
            try:
                state['task'] = ctx.spawn_task(module.Host(service, descriptor).serve(), name='task-handoff-host')
            finally:
                reset_hermes_home_override(token)
            # Observe bind/identity errors; readiness is always read from the socket,
            # never inferred from a plugin-loaded acknowledgement.
            def finished(task):
                if not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        logger.error('Task handoff host unavailable: %s; socket readiness is required', type(error).__name__)
            state['task'].add_done_callback(finished)
        loop.call_soon_threadsafe(start)

    ctx.on_unload(cancel)
    ctx.register_hook('post_gateway_admission', attach)
    attach()
