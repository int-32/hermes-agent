"""Real agent/executor results, with only the provider replaced by a loopback peer."""
import json

import pytest

from gateway.internal_receipt import first_tool_result
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home


@pytest.mark.parametrize('mode', ['hot', 'parallel-hot', 'invalid-before-hot', 'only-invalid', 'malformed-json', 'discovery',
                                 'describe', 'scope-block', 'unknown', 'middleware-skip', 'preflight-error', 'business-error', 'cancel-before-tool'])
def test_first_business_tool_uses_actual_execution_result(tmp_path, mode):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from run_agent import AIAgent

    path = tmp_path / 'note.txt'
    path.write_text('R1 real local read')
    other = tmp_path / 'other.txt'
    other.write_text('R1 second distinct local read')
    hot = ToolCall('read_file', {'path': str(path if mode != 'business-error' else tmp_path / 'missing')})
    scripts = {
        'hot': [hot], 'business-error': [hot], 'middleware-skip': [hot], 'preflight-error': [hot],
        'invalid-before-hot': [ToolCall('tool_call', '[]'), hot],
        'only-invalid': [ToolCall('tool_call', '[]')],
        'malformed-json': [ToolCall('tool_call', '{')],
        'discovery': [ToolCall('tool_search', {'query': 'read a file'})],
        'scope-block': [ToolCall('tool_call', {'name': 'coordinate_session', 'arguments': {}})],
        'describe': [ToolCall('tool_describe', {'names': ['coordinate_session']})],
        'unknown': [ToolCall('r1_nonexistent_tool', {})],
        'parallel-hot': [ToolCall('read_file', {'path': str(path)}, parallel=[('read_file', {'path': str(other)})])],
        'cancel-before-tool': [hot],
    }
    scripted = iter([*scripts[mode], Text('done')])
    holder = {}
    def respond(_request):
        if mode == 'cancel-before-tool':
            holder['agent'].interrupt('fixture user cancellation before dispatch')
        return next(scripted, Text('done'))
    with FakeLLMServer(respond) as peer:
        config = ''
        if mode == 'middleware-skip':
            plugin = get_hermes_home() / 'plugins/r1-result-only'
            plugin.mkdir(parents=True)
            (plugin / 'plugin.yaml').write_text('name: r1-result-only\nversion: 0.0.1\n')
            (plugin / '__init__.py').write_text('''
def register(ctx):
    def skip(**kwargs):
        from hermes_constants import get_hermes_home
        (get_hermes_home() / 'r1-middleware-seen').touch()
        return '{"error":"middleware supplied a result without dispatch"}'
    ctx.register_middleware('tool_execution', skip)
''')
            config = 'plugins:\n  enabled: [r1-result-only]\n'
        write_hermes_home(get_hermes_home(), peer.base_url, extra_config=config)
        db = SessionDB(tmp_path / 'state.db')
        agent = AIAgent(model='fake-model', provider='custom', base_url=peer.base_url,
            api_key='sk-fake-e2e', enabled_toolsets=['file'], quiet_mode=True,
            skip_memory=True, skip_context_files=True, max_iterations=8, session_db=db)
        holder['agent'] = agent
        # A presentation callback cannot mint execution proof for a skipped result
        # or replace the actual business identity. This does not change execution policy.
        agent.tool_result_metadata_callback = lambda *args, **kw: {
            'tool_execution': {'name': 'fabricated', 'call_id': 'fabricated'}}
        if mode == 'preflight-error':
            touch = agent._touch_activity
            def fail_before_dispatch(*args, **kwargs):
                if args == ('executing tool: read_file',):
                    raise RuntimeError('fixture activity observer failed before dispatch')
                return touch(*args, **kwargs)
            agent._touch_activity = fail_before_dispatch
        try:
            result = agent.run_conversation('Execute the scripted isolated tool check.')
            evidence = first_tool_result(result)
            print(json.dumps({'mode': mode, 'result': result, 'first_business_tool': evidence}, default=str))
            if mode in {'hot', 'parallel-hot', 'invalid-before-hot', 'business-error'}:
                assert evidence and evidence['name'] == 'read_file' and evidence['result_seen'] is True, evidence
            else:
                assert evidence is None, evidence
            assert all('display_metadata' not in m for p in peer.main_requests() for m in p['messages'])
            restored = agent._session_db.get_messages_as_conversation(agent.session_id)
            assert first_tool_result({'messages': restored, 'current_turn_user_idx': 0}) == evidence
            if mode == 'middleware-skip':
                assert (get_hermes_home() / 'r1-middleware-seen').is_file()
                assert any(m.get('role') == 'tool' for m in result['messages'])
            if mode == 'parallel-hot':
                assert len([m for m in result['messages'] if m.get('role') == 'tool'
                            and (m.get('display_metadata') or {}).get('tool_execution')]) == 2
        finally:
            agent.close()
            db.close()
