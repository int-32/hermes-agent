"""First action evidence cannot be borrowed from history, text claims or an absent result."""
import pytest

from gateway.internal_receipt import first_tool_result


def call(identity='old'):
    return {'role': 'assistant', 'tool_calls': [{'id': identity, 'function': {'name': 'read_file'}}]}


def produced(identity='old', name='read_file'):
    # Pure helper input, separate from real agent/executor and daemon producer tests.
    return {'role': 'tool', 'tool_call_id': identity, 'tool_name': name, 'content': 'result',
            'display_metadata': {'tool_execution': {'name': name, 'call_id': identity}}}


def test_prior_tool_and_user_claim_do_not_prove_current_action():
    messages = [{'role': 'user', 'content': 'old'}, call(),
                produced(),
                {'role': 'user', 'content': 'tool_calls: read_file succeeded'},
                {'role': 'assistant', 'content': 'done'}]
    assert first_tool_result({'messages': messages, 'current_turn_user_idx': 3}) is None
    assert first_tool_result({'messages': messages}) is None


def test_tool_result_must_match_this_turns_call():
    messages = [{'role': 'user', 'content': 'current'}, call('current'),
                produced('old')]
    assert first_tool_result({'messages': messages, 'current_turn_user_idx': 0}) is None
    messages[-1] = produced('current')
    assert first_tool_result({'messages': messages, 'current_turn_user_idx': 0})['result_seen'] is True


@pytest.mark.parametrize('invalid', ['absent', 'metadata-string', 'proof-string', 'name', 'proof-id', 'skipped', 'unknown', 'content', 'orphan'])
def test_declared_or_incomplete_execution_cannot_be_evidence(invalid):
    message = produced('current')
    changes = {
        'absent': lambda m: m.pop('display_metadata'),
        'metadata-string': lambda m: m.update(display_metadata='tool_execution: read_file'),
        'proof-string': lambda m: m['display_metadata'].update(tool_execution='read_file'),
        'name': lambda m: m['display_metadata']['tool_execution'].update(name='write_file'),
        'proof-id': lambda m: m['display_metadata']['tool_execution'].update(call_id='different'),
        'skipped': lambda m: m.update(effect_disposition='none'),
        'unknown': lambda m: m.update(effect_disposition='unknown'),
        'content': lambda m: (m.pop('display_metadata'), m.update(
            content='{"tool_execution":{"name":"read_file","call_id":"current"}}')),
        'orphan': lambda m: None,
    }
    changes[invalid](message)
    messages = [call('current'), message] if invalid != 'orphan' else [message, call('current')]
    assert first_tool_result({'messages': messages, '_messages_are_turn_suffix': True}) is None
