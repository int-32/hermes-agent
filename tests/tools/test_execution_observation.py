"""Business-entry observation without altering registry policy or results."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from tools.registry import ToolRegistry, collect_tool_execution, invoke_tool_handler


def test_lookup_and_argument_binding_cannot_mint_entry():
    registry = ToolRegistry()
    with collect_tool_execution('missing', 'id') as missing:
        assert 'error' in json.loads(registry.dispatch('missing', {}))
    assert not missing.entered
    def requires_two(first, second):
        raise AssertionError('invalid binding must never enter')
    with collect_tool_execution('narrow', 'id') as invalid:
        with pytest.raises(TypeError):
            invoke_tool_handler('narrow', requires_two, {})
    assert not invalid.entered


def test_async_starts_on_await_and_business_error_is_an_attempt():
    entered = []
    async def handler(args):
        entered.append(args)
        return '{"error":"business error"}'
    with collect_tool_execution('async', 'id') as execution:
        pending = invoke_tool_handler('async', handler, {'value': 1}, _await_result=True)
        assert not execution.entered and not entered
        assert json.loads(asyncio.run(pending))['error'] == 'business error'
        assert execution.entered and entered == [{'value': 1}]


def test_registry_sync_and_async_entries_preserve_results(tmp_path):
    registry = ToolRegistry()
    def write(args):
        (tmp_path / args['name']).write_text('entered')
        return '{"ok":true}'
    async def async_write(args):
        return write(args)
    for name, handler, asynchronous in [('sync', write, False), ('async', async_write, True)]:
        registry.register(name=name, toolset='fixture', schema={'name': name}, handler=handler, is_async=asynchronous)
        with collect_tool_execution(name, name + '-id') as execution:
            assert json.loads(registry.dispatch(name, {'name': name})) == {'ok': True}
        assert execution.entered and (tmp_path / name).read_text() == 'entered'


def test_parallel_collectors_and_nested_exception_are_isolated():
    registry = ToolRegistry()
    barrier = threading.Barrier(2)
    threads = set()
    def read(args):
        threads.add(threading.get_ident())
        barrier.wait(timeout=10)
        return json.dumps(args)
    registry.register(name='read', toolset='fixture', schema={'name': 'read'}, handler=read)
    def run(identity):
        with collect_tool_execution('read', identity) as outer:
            with pytest.raises(RuntimeError), collect_tool_execution('other', 'inner') as inner:
                invoke_tool_handler('unmatched', lambda: 'unmatched')
                raise RuntimeError('fixture exception restores outer context')
            assert not inner.entered and not outer.entered
            assert json.loads(registry.dispatch('read', {'id': identity})) == {'id': identity}
        return outer
    with ThreadPoolExecutor(max_workers=2) as pool:
        executions = list(pool.map(run, ['one', 'two']))
    assert len(threads) == 2
    assert [(e.call_id, e.entered) for e in executions] == [('one', True), ('two', True)]
