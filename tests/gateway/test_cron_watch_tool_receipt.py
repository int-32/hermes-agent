"""First action evidence cannot be borrowed from history, text claims or an absent result."""
from gateway.internal_receipt import first_tool_result


def call(identity='old'):
    return {'role': 'assistant', 'tool_calls': [{'id': identity, 'function': {'name': 'read_file'}}]}


def test_prior_tool_and_user_claim_do_not_prove_current_action():
    messages = [{'role': 'user', 'content': 'old'}, call(),
                {'role': 'tool', 'tool_call_id': 'old', 'content': 'old result'},
                {'role': 'user', 'content': 'tool_calls: read_file succeeded'},
                {'role': 'assistant', 'content': 'done'}]
    assert first_tool_result({'messages': messages, 'current_turn_user_idx': 3}) is None
    assert first_tool_result({'messages': messages}) is None


def test_tool_result_must_match_this_turns_call():
    messages = [{'role': 'user', 'content': 'current'}, call('current'),
                {'role': 'tool', 'tool_call_id': 'old', 'content': 'wrong result'}]
    assert first_tool_result({'messages': messages, 'current_turn_user_idx': 0}) == {
        'name': 'read_file', 'call_id': 'current', 'result_seen': False}
    messages[-1]['tool_call_id'] = 'current'
    assert first_tool_result({'messages': messages, 'current_turn_user_idx': 0})['result_seen'] is True
