"""Actual ACP approval and business handlers, with only client/provider I/O replaced."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from gateway.internal_receipt import first_tool_result
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home


@pytest.mark.parametrize('decision', ['deny', 'requester-error', 'allow_once'])
@pytest.mark.parametrize('layout', ['sequential', 'batch'])
def test_acp_refusal_is_not_handler_execution(tmp_path, decision, layout):
    from acp_adapter.edit_approval import (
        make_acp_edit_approval_requester, reset_edit_approval_requester, set_edit_approval_requester,
    )
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools.registry import registry

    target, note = tmp_path / 'edit.txt', tmp_path / 'note.txt'
    if decision != 'allow_once':
        target.write_text('before')
    note.write_text('actual read')
    write = ToolCall('write_file', {'path': str(target), 'content': 'after'})
    read = ToolCall('read_file', {'path': str(note)})
    scripts = [write, read, Text('done')] if layout == 'sequential' else [
        ToolCall('write_file', {'path': str(target), 'content': 'after'},
                 parallel=[('read_file', {'path': str(note)})]), Text('done')]
    approvals, handlers = [], []
    async def permission(*args, **kwargs):
        approvals.append(kwargs)
        if decision == 'requester-error':
            raise RuntimeError('fixture ACP client disconnected')
        return SimpleNamespace(outcome=SimpleNamespace(outcome='selected', option_id=decision))

    with FakeLLMServer(scripts) as peer:
        write_hermes_home(get_hermes_home(), peer.base_url)
        db = SessionDB(tmp_path / 'state.db')
        agent = AIAgent(model='fake-model', provider='custom', base_url=peer.base_url,
            api_key='sk-fake-e2e', platform='acp', enabled_toolsets=['file'], quiet_mode=True,
            skip_memory=True, skip_context_files=True, max_iterations=6, session_db=db)
        code = registry.get_entry('write_file').handler.__code__
        def profile(frame, event, arg):
            if event == 'call' and frame.f_code is code:
                handlers.append(threading.get_ident())
        async def run():
            requester = make_acp_edit_approval_requester(permission, asyncio.get_running_loop(), agent.session_id,
                auto_approve_getter=lambda: ('ask', str(tmp_path)))
            token = set_edit_approval_requester(requester)
            try:
                return await asyncio.to_thread(agent.run_conversation, 'Perform the isolated ACP edit and read.')
            finally:
                reset_edit_approval_requester(token)
        previous = threading.getprofile()
        threading.setprofile(profile)
        try:
            result = asyncio.run(run())
            evidence = first_tool_result(result)
            tools = [m for m in result['messages'] if m.get('role') == 'tool']
            print(json.dumps(dict(decision=decision, layout=layout, approvals=len(approvals),
                handler_calls=len(handlers), target=target.read_text(), result=result, first_tool=evidence), default=str))
            assert len(approvals) == 1
            assert target.read_text() == ('after' if decision == 'allow_once' else 'before')
            assert len(handlers) == (1 if decision == 'allow_once' else 0)
            assert evidence and evidence['name'] == ('write_file' if decision == 'allow_once' else 'read_file'), evidence
            write_result = next(m for m in tools if m.get('tool_name') == 'write_file')
            proof = (write_result.get('display_metadata') or {}).get('tool_execution')
            assert bool(proof) is (decision == 'allow_once'), proof
            assert first_tool_result({'messages': db.get_messages_as_conversation(agent.session_id),
                                      'current_turn_user_idx': result['current_turn_user_idx']}) == evidence
            assert all('display_metadata' not in m for p in peer.main_requests() for m in p['messages'])
        finally:
            threading.setprofile(previous)
            agent.close()
            db.close()


def test_inline_business_entry_has_real_store_effect(tmp_path):
    from hermes_constants import get_hermes_home
    from run_agent import AIAgent
    with FakeLLMServer([ToolCall('tool_call', {'name': 'todo_list', 'arguments': {'todos': [
        {'id': 'fixture', 'content': 'local inline control', 'status': 'pending'}]}}), Text('done')]) as peer:
        write_hermes_home(get_hermes_home(), peer.base_url)
        agent = AIAgent(model='fake-model', provider='custom', base_url=peer.base_url,
            api_key='sk-fake-e2e', enabled_toolsets=['file', 'todo'], quiet_mode=True,
            skip_memory=True, skip_context_files=True, max_iterations=4)
        try:
            assert 'tool_call' in agent.valid_tool_names
            result = agent.run_conversation('Record one local fixture todo.')
            assert agent._todo_store.read() == [
                {'id': 'fixture', 'content': 'local inline control', 'status': 'pending'}]
            assert first_tool_result(result)['name'] == 'todo_list'
            assert all('display_metadata' not in m for p in peer.main_requests() for m in p['messages'])
        finally:
            agent.close()
