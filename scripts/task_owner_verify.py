"""Read-only verification of one collector projection, full artifact and original v2 reference.

Exit 0: mechanically ready for owner processing; 3: reconciliation required;
2: invalid/unreadable evidence. No result accepts business work or advances K.
"""
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
import re
import stat

from scripts import task_owner_snapshot as collector

INPUT_BYTES = 64 * 1024 * 1024
DELTA_KEYS = ('tasks', 'executors', 'removed_tasks', 'removed_executors')
CHECKPOINT_SCHEMA = 'task-owner-inspection-checkpoint/v2'


class InvalidEvidence(ValueError):
    """Stable diagnostic codes, never raw exception/task text."""


def require(condition, code='unsupported_observation_shape'):
    if not condition:
        raise InvalidEvidence(code)


def fields(value, keys):
    require(isinstance(value, dict) and all(k in value for k in keys))


def sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def integer(value):
    return type(value) is int and value >= 0


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def scalar(value):
    return value is None or isinstance(value, str) or finite(value)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'duplicate_json_key')
        value[key] = item
    return value


def decode(entry):
    if 'json' not in entry:
        entry['json'] = json.loads(entry['raw'].decode('utf-8-sig'), object_pairs_hook=unique_object,
                                  parse_constant=lambda value: require(False, 'nonfinite_json'))
    return entry['json']


def read_file(supplied, cache, limit=INPUT_BYTES):
    path = Path(supplied)
    require(path.is_absolute(), 'input_path_not_absolute')
    resolved = path.resolve(strict=True)
    descriptor = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), 'input_not_regular')
        require(before.st_size <= limit, 'input_too_large')
        key = (before.st_dev, before.st_ino)
        stamp = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if key in cache:
            require(cache[key]['stamp'] == stamp, 'input_changed')
        else:
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            require(len(raw) == before.st_size and stamp == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                    'input_changed')
            cache[key] = dict(raw=raw, stamp=stamp, sha256=hashlib.sha256(raw).hexdigest())
    entry = cache[key]
    return entry, dict(path=str(path), sha256=entry['sha256'], bytes=len(entry['raw']))


def cursor(value):
    fields(value, ('identity', 'offset', 'anchor'))
    require(integer(value['offset']) and sha(value['anchor']) and isinstance(value['identity'], list)
            and len(value['identity']) == 2 and all(integer(v) for v in value['identity']))


def completion(value):
    fields(value, ('key', 'subtype', 'summary', 'consumption', 'delivery', 'next_action', 'evidence'))
    require(sha(value['key']) and all(isinstance(value[k], str) for k in value if k != 'key'))
    require(len(value['subtype']) <= 80 and len(value['summary']) <= 600)
    require(value['consumption'] == value['delivery'] == 'UNVERIFIED')


def boards(value, *, previous=False):
    require(isinstance(value, list))
    seen = set()
    for board in value:
        fields(board, ('database', 'tasks') if previous else ('database', 'profile_home', 'tasks'))
        require(isinstance(board['database'], str) and board['database'] not in seen and isinstance(board['tasks'], list))
        if not previous:
            require(isinstance(board['profile_home'], str))
        seen.add(board['database'])
        for task in board['tasks']:
            fields(task, ('id',) if previous else ('id', 'title', 'status', 'project_id', 'assignee', 'session_id',
                'workspace_path', 'last_failure_error', 'card_digest', 'history_digest'))
            require(type(task['id']) in (str, int))
            require(all(sha(task[k]) for k in ('card_digest', 'history_digest') if k in task))
            if not previous:
                require(isinstance(task['status'], str) and all(scalar(task[k]) for k in
                    ('title', 'project_id', 'assignee', 'session_id', 'workspace_path', 'last_failure_error')))
                if 'untrusted_lookup_hints' in task:
                    require(isinstance(task['untrusted_lookup_hints'], dict))
        require(len({task['id'] for task in board['tasks']}) == len(board['tasks']))


def executors(value, *, previous=False):
    require(isinstance(value, list))
    seen = set()
    for worker in value:
        fields(worker, ('id',) if previous else ('id', 'status', 'cwd', 'thread_id', 'session_id', 'pid_hint',
            'metadata_digest', 'event_size', 'result_state', 'has_more', 'age_is_not_a_blockage_verdict',
            'observation_older_than_30m'))
        require(isinstance(worker['id'], str) and worker['id'] not in seen)
        seen.add(worker['id'])
        if not previous:
            require(sha(worker['metadata_digest']) and type(worker['has_more']) is bool
                    and worker['age_is_not_a_blockage_verdict'] is True
                    and type(worker['observation_older_than_30m']) is bool)
            require(worker['event_size'] is None or integer(worker['event_size']))
            require(worker['result_state'] in ('UNVERIFIED', 'executing', 'execution_ended'))
            require(all(scalar(worker[k]) for k in ('status', 'cwd', 'thread_id', 'session_id', 'pid_hint')))
        if worker.get('turn_key') is not None:
            require(sha(worker['turn_key']))
        if worker.get('event_cursor') is not None:
            cursor(worker['event_cursor'])
        if worker.get('last_completion') is not None:
            completion(worker['last_completion'])
        if worker.get('turn_completion_keys') is not None:
            keys = worker['turn_completion_keys']
            require(isinstance(keys, list) and len(keys) <= collector.TURN_COMPLETION_LIMIT and all(sha(v) for v in keys))
        if 'event_read_error' in worker:
            require(type(worker['event_read_error']) is bool)


def errors(value):
    require(isinstance(value, list))
    for error in value:
        fields(error, ('source', 'error', 'reason'))
        require(all(isinstance(error[k], str) for k in ('source', 'error', 'reason')))


def delta(value):
    fields(value, DELTA_KEYS)
    require(set(value) == set(DELTA_KEYS) and all(isinstance(value[k], list) for k in DELTA_KEYS))
    for task in value['tasks']:
        fields(task, ('database', 'id', 'phase', 'changes', 'unique_wait', 'wait_owner', 'next_action', 'evidence', 'history_tail'))
        require(isinstance(task['database'], str) and type(task['id']) in (str, int)
                and isinstance(task['phase'], str) and isinstance(task['changes'], list)
                and set(task['changes']) <= {'card_digest', 'history_digest'})
        require(task['unique_wait'] == task['wait_owner'] == 'UNVERIFIED'
                and isinstance(task['history_tail'], list) and len(task['history_tail']) <= 2
                and all(isinstance(task[k], str) for k in ('next_action', 'evidence')))
        for history in task['history_tail']:
            fields(history, ('table',))
            require(all(isinstance(v, str) for v in history.values()))
    for worker in value['executors']:
        fields(worker, ('id', 'result_state', 'completions', 'omitted_completions', 'wait_owner', 'next_action', 'evidence'))
        require(isinstance(worker['id'], str) and isinstance(worker['completions'], list)
                and len(worker['completions']) <= 2 and integer(worker['omitted_completions'])
                and worker['wait_owner'] == 'UNVERIFIED'
                and worker['result_state'] in ('UNVERIFIED', 'executing', 'execution_ended')
                and all(isinstance(worker[k], str) for k in ('next_action', 'evidence')))
        for item in worker['completions']:
            completion(item)
    for item in value['removed_tasks']:
        fields(item, ('database', 'id'))
        require(isinstance(item['database'], str) and type(item['id']) in (str, int))
    require(all(isinstance(v, str) for v in value['removed_executors']))


def observation(value):
    fields(value, ('schema', 'boards', 'executors', 'errors', 'delta', 'coverage', 'observed_at',
                   'next_review_at', 'reasons', 'wakeAgent', 'checkpoint_meaning'))
    require(value['schema'] == collector.SCHEMA, 'unsupported_snapshot_schema')
    boards(value['boards']); executors(value['executors']); errors(value['errors']); delta(value['delta'])
    require(finite(value['observed_at']) and finite(value['next_review_at'])
            and value['next_review_at'] >= value['observed_at'])
    require(type(value['wakeAgent']) is bool and isinstance(value['reasons'], list)
            and all(isinstance(v, str) for v in value['reasons']))
    require(isinstance(value['coverage'], str) and bool(value['coverage'])
            and isinstance(value['checkpoint_meaning'], str) and bool(value['checkpoint_meaning']))
    collector.task_index(value)


def reference(checkpoint, cache):
    fields(checkpoint, ('schema', 'observed_at', 'consumed_fingerprints'))
    require(checkpoint['schema'] == CHECKPOINT_SCHEMA, 'unsupported_checkpoint_schema')
    fields(checkpoint['consumed_fingerprints'], ('snapshot_path',))
    require(isinstance(checkpoint['observed_at'], str), 'invalid_checkpoint_clock')
    stamp = datetime.fromisoformat(checkpoint['observed_at'])
    require(finite(stamp.timestamp()), 'invalid_checkpoint_clock')
    entry, facts = read_file(checkpoint['consumed_fingerprints']['snapshot_path'], cache)
    baseline = decode(entry)
    fields(baseline, ('schema', 'boards', 'executors'))
    require(baseline['schema'] == collector.SCHEMA, 'unsupported_snapshot_schema')
    boards(baseline['boards'], previous=True); executors(baseline['executors'], previous=True)
    collector.task_index(baseline)
    if 'errors' in baseline:
        errors(baseline['errors'])
    if 'observed_at' in baseline:
        require(finite(baseline['observed_at']), 'invalid_baseline_clock')
    deadline = baseline.get('next_review_at', stamp.timestamp() + collector.REVIEW_SECONDS)
    require(finite(deadline), 'invalid_baseline_clock')
    return baseline, facts, deadline, stamp.tzinfo is not None


def redacted_projection(value):
    from agent.redact import redact_sensitive_text
    raw = collector.encoded(value, ensure_ascii=True)
    return json.loads(redact_sensitive_text(raw, force=True), object_pairs_hook=unique_object,
                      parse_constant=lambda value: require(False, 'nonfinite_json'))


def projection_matches(actual, expected, path=()):
    # Only root schema clocks share numeric int/float semantics. In particular,
    # false/0 differ at the native gate, and counts must retain their JSON type.
    if path in (('observed_at',), ('next_review_at',)):
        return finite(actual) and actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            projection_matches(actual[key], value, (*path, key)) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            projection_matches(left, right, (*path, index)) for index, (left, right) in enumerate(zip(actual, expected)))
    return actual == expected


def unresolved(full, projection, baseline, zoned):
    counts = dict(observation_errors=len(full['errors']), baseline_errors=len(baseline.get('errors', [])),
                  projected_omitted=sum(projection['omitted'].values()), omitted_errors=projection['omitted_errors'],
                  omitted_completions=sum(v['omitted_completions'] for v in full['delta']['executors']),
                  event_backlog_workers=sum(v['has_more'] for v in full['executors']),
                  event_read_errors=sum(v.get('event_read_error', False) for v in full['executors']))
    issues = [key for key, count in counts.items() if count]
    if 'errors' not in baseline:
        issues.append('baseline_health_unknown')
    if not zoned:
        issues.append('checkpoint_timezone_unknown')
    if set(full['reasons']) & {'read_error', 'snapshot_write_error'}:
        issues.append('read_error_reason')
    if full.get('truncated') or full.get('requires_reconciliation') or full.get('complete') is False:
        issues.append('full_content_marked_incomplete')
    return counts, issues


def verify(projection_path, checkpoint_path, *, stdin=None, cache=None):
    report = dict(status='invalid', evidence_verified=False, ready_for_processing=False, issues=[],
                  consumption='UNVERIFIED', native_ack='UNVERIFIED', checkpoint_written=False,
                  collection_baseline_binding='UNVERIFIED_NOT_IN_OBSERVATION_FORMAT', full_content_returned=False)
    cache = {} if cache is None else cache
    try:
        if str(projection_path) == '-':
            raw = (sys.stdin.buffer if stdin is None else stdin).read(collector.PROJECTION_BYTES + 1)
            require(len(raw) <= collector.PROJECTION_BYTES, 'projection_too_large')
            incoming = decode(dict(raw=raw))
        else:
            entry, report['projection_input'] = read_file(projection_path, cache, collector.PROJECTION_BYTES)
            incoming = decode(entry)
        fields(incoming, ('snapshot_artifact',))
        artifact = incoming['snapshot_artifact']
        fields(artifact, ('path', 'bytes', 'sha256', 'observation_only'))
        require(set(artifact) == {'path', 'bytes', 'sha256', 'observation_only'}
                and isinstance(artifact['path'], str) and sha(artifact['sha256']) and integer(artifact['bytes'])
                and artifact['observation_only'] is True, 'invalid_artifact_reference')
        # Verify exact bytes before decoding; reuse those bytes for every structural check.
        entry, facts = read_file(artifact['path'], cache)
        require(facts['bytes'] == artifact['bytes'], 'artifact_bytes_mismatch')
        require(facts['sha256'] == artifact['sha256'], 'artifact_hash_mismatch')
        full = decode(entry); observation(full)
        expected = collector.project_observation(full, artifact)
        require(projection_matches(redacted_projection(incoming), redacted_projection(expected)), 'projection_mismatch')
        report['snapshot_artifact'] = artifact
        checkpoint_entry, report['checkpoint'] = read_file(checkpoint_path, cache)
        baseline, report['consumed_snapshot'], deadline, zoned = reference(decode(checkpoint_entry), cache)
        counts, report['issues'] = unresolved(full, expected, baseline, zoned)
        report.update(evidence_verified=True, unresolved=counts,
            reference_relation='same_file' if os.path.samefile(artifact['path'], report['consumed_snapshot']['path']) else 'distinct',
            baseline_review_deadline=deadline, projection_comparison='raw_or_cron_redacted',
            coverage=dict(boards=len(full['boards']), tasks=len(collector.task_index(full)), workers=len(full['executors'])),
            ready_for_processing=not report['issues'], status='requires_reconciliation' if report['issues'] else 'ready_for_processing')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, RuntimeError) as exc:
        report['issues'] = [str(exc) if isinstance(exc, InvalidEvidence) else type(exc).__name__]
    return report


def render(report):
    """Bound the report, never print the full observation or secret-bearing task text."""
    output = collector.encoded(report, ensure_ascii=True)
    if len(output) + 1 > collector.PROJECTION_BYTES:
        report.update(status='requires_reconciliation' if report['evidence_verified'] else 'invalid',
                      ready_for_processing=False, report_truncated=True)
        report['issues'].append('report_paths_omitted')
        for field in ('snapshot_artifact', 'checkpoint', 'consumed_snapshot', 'projection_input'):
            if field in report and 'path' in report[field]:
                item = dict(report[field]); item['omitted_path_chars'] = len(item.pop('path'))
                report[field] = item
        output = collector.encoded(report, ensure_ascii=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projection', required=True, help='absolute cached JSON path, or - for JSON stdin')
    parser.add_argument('--checkpoint', required=True, type=Path, help='absolute original v2 checkpoint; never written')
    args = parser.parse_args()
    report = verify(args.projection, args.checkpoint)
    print(render(report))
    return 0 if report['ready_for_processing'] else 3 if report['evidence_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
