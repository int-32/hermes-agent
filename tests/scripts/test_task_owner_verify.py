"""Read the real collector's bytes once and verify evidence without accepting work."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.scripts.test_task_owner_snapshot import adapter_cli, saved_baseline, worker

pytest_plugins = ['tests.scripts.test_task_owner_snapshot', 'tests.scripts.test_task_owner_gate']

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/task_owner_verify.py'


@pytest.fixture
def evidence(home):
    worker(home, events=[{'kind': 'turn_done', 'text': 'offline result\u2028\u0085'}])
    baseline, checkpoint = saved_baseline(home)
    artifact = home.parent / 'candidate.json'
    proc, projection = adapter_cli(home, artifact, '--checkpoint', checkpoint)
    assert proc.returncode == 0
    projection_path = home.parent / 'projection.json'
    projection_path.write_text(json.dumps(projection, ensure_ascii=True))
    return SimpleNamespace(home=home, baseline=baseline, checkpoint=checkpoint,
        artifact=artifact, projection=projection, projection_path=projection_path)


def invoke(evidence, *, stdin=False):
    before = {p: p.read_bytes() for p in (evidence.artifact, evidence.baseline, evidence.checkpoint)
              if p.is_file()}
    proc = subprocess.run([sys.executable, str(SCRIPT), '--projection', '-' if stdin else str(evidence.projection_path),
        '--checkpoint', str(evidence.checkpoint)], cwd=ROOT, capture_output=True, text=True, check=False,
        input=evidence.projection_path.read_text() if stdin else None,
        env=dict(os.environ, HOME=str(evidence.home.parent), HERMES_HOME=str(evidence.home)), timeout=15)
    assert proc.stdout, proc.stderr
    assert proc.stdout.isascii() and len(proc.stdout.splitlines()) == 1 and len(proc.stdout.encode()) <= 16384
    assert {p: p.read_bytes() for p in before} == before
    return proc.returncode, json.loads(proc.stdout)


def rewrite(evidence, mutate):
    full = json.loads(evidence.artifact.read_bytes())
    mutate(full)
    raw = (json.dumps(full, ensure_ascii=True) + '\n').encode()
    evidence.artifact.write_bytes(raw)
    from scripts.task_owner_snapshot import project_observation
    ref = dict(path=str(evidence.artifact.resolve()), sha256=hashlib.sha256(raw).hexdigest(),
               bytes=len(raw), observation_only=True)
    evidence.projection = project_observation(full, ref)
    evidence.projection_path.write_text(json.dumps(evidence.projection, ensure_ascii=True))


@pytest.mark.parametrize('stdin', [False, True])
def test_real_artifact_and_original_v2_reference_are_ready_only_for_processing(evidence, stdin):
    code, report = invoke(evidence, stdin=stdin)
    assert code == 0 and report['ready_for_processing'] is True
    assert report['evidence_verified'] is True and report['issues'] == []
    assert report['snapshot_artifact'] == evidence.projection['snapshot_artifact']
    assert report['reference_relation'] == 'distinct'
    assert report['consumption'] == report['native_ack'] == 'UNVERIFIED'
    assert report['checkpoint_written'] is False
    assert report['collection_baseline_binding'] == 'UNVERIFIED_NOT_IN_OBSERVATION_FORMAT'


@pytest.mark.parametrize('defect', ['hash', 'bytes', 'relative-path', 'missing-artifact', 'missing-reference',
                                   'checkpoint-schema', 'baseline-schema', 'projection-truncated'])
def test_missing_or_mismatched_identity_schema_or_reference_is_not_healthy(evidence, defect):
    changes = {
        'hash': lambda: evidence.projection['snapshot_artifact'].update(sha256='0'*64),
        'bytes': lambda: evidence.projection['snapshot_artifact'].update(bytes=1),
        'relative-path': lambda: evidence.projection['snapshot_artifact'].update(path='candidate.json'),
        'missing-artifact': evidence.artifact.unlink,
        'missing-reference': evidence.baseline.unlink,
        'checkpoint-schema': lambda: evidence.checkpoint.write_text('{"schema":"unsupported"}'),
        'baseline-schema': lambda: evidence.baseline.write_text('{"schema":"unsupported"}'),
        'projection-truncated': lambda: evidence.projection_path.write_text('{'),
    }
    changes[defect]()
    if defect != 'projection-truncated':
        evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and report['ready_for_processing'] is False and report['issues']
    assert report['consumption'] == report['native_ack'] == 'UNVERIFIED'


@pytest.mark.parametrize('field', ['schema', 'boards', 'executors', 'errors', 'delta', 'coverage',
                                  'observed_at', 'next_review_at', 'reasons', 'wakeAgent', 'checkpoint_meaning'])
def test_complete_observation_cannot_omit_producer_fields(evidence, field):
    # Keep the transport identity valid while making the full object structurally incomplete.
    full = json.loads(evidence.artifact.read_bytes())
    full.pop(field)
    raw = json.dumps(full).encode()
    evidence.artifact.write_bytes(raw)
    evidence.projection['snapshot_artifact'].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and not report['ready_for_processing']


@pytest.mark.parametrize('case', ['errors-null', 'task-digest', 'cursor', 'worker-backlog-type', 'nonfinite-clock', 'overflow-clock'])
def test_malformed_nested_observation_is_not_accepted(evidence, case):
    changes = {
        'errors-null': lambda full: full.update(errors=None),
        'task-digest': lambda full: full['boards'][0]['tasks'][0].update(card_digest='wrong'),
        'cursor': lambda full: full['executors'][0]['event_cursor'].update(offset=-1),
        'worker-backlog-type': lambda full: full['executors'][0].update(has_more='false'),
        'nonfinite-clock': lambda full: full.update(observed_at=float('nan')),
        'overflow-clock': lambda full: full.update(observed_at=10**1000),
    }
    full = json.loads(evidence.artifact.read_bytes()); changes[case](full)
    raw = json.dumps(full).encode(); evidence.artifact.write_bytes(raw)
    evidence.projection['snapshot_artifact'].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and not report['ready_for_processing']


@pytest.mark.parametrize('issue', ['errors', 'omitted', 'backlog', 'completion-omitted'])
def test_valid_but_unresolved_observation_is_explicit_not_consumed(evidence, issue):
    def mutate(full):
        if issue == 'errors':
            full['errors'].append(dict(source='known-missing.db', error='OSError', reason='previous_board_unavailable'))
            full['reasons'].append('read_error');full['wakeAgent'] = True
        elif issue == 'omitted':
            full['delta']['removed_executors'] = ['removed-' + str(i) for i in range(40)]
        elif issue == 'backlog':
            full['executors'][0]['has_more'] = True
        else:
            full['delta']['executors'] = [dict(id='worker-a', result_state='execution_ended', completions=[],
                omitted_completions=2, wait_owner='UNVERIFIED', next_action='reconcile_with_original_task_owner', evidence='worker-a')]
    rewrite(evidence, mutate)
    code, report = invoke(evidence)
    assert code == 3 and report['evidence_verified'] and not report['ready_for_processing']
    assert report['issues'] and report['checkpoint_written'] is False


def test_self_reported_processed_is_not_consumption_or_ack(evidence):
    rewrite(evidence, lambda full: full.update(processed=True, native_ack=True))
    checkpoint = json.loads(evidence.checkpoint.read_text());checkpoint['processed'] = True
    evidence.checkpoint.write_text(json.dumps(checkpoint))
    code, report = invoke(evidence)
    assert code == 0 and report['consumption'] == report['native_ack'] == 'UNVERIFIED'
    assert report['checkpoint_written'] is False


@pytest.mark.parametrize('defect', ['board-profile', 'task-title', 'worker-cwd', 'delta-result-state'])
def test_known_nested_fields_have_supported_types(evidence, defect):
    changes = {
        'board-profile': lambda full: full['boards'][0].update(profile_home={}),
        'task-title': lambda full: full['boards'][0]['tasks'][0].update(title=[]),
        'worker-cwd': lambda full: full['executors'][0].update(cwd={}),
        'delta-result-state': lambda full: full['delta'].update(executors=[dict(id='worker-a', result_state=False,
            completions=[], omitted_completions=0, wait_owner='UNVERIFIED', next_action='reconcile', evidence='worker-a')]),
    }
    rewrite(evidence, changes[defect])
    code, report = invoke(evidence)
    assert code == 2 and not report['ready_for_processing']


@pytest.mark.parametrize('mutation', ['coverage', 'omitted', 'requires-reconciliation'])
def test_projection_cannot_hide_full_coverage_or_truncation(evidence, mutation):
    changes = {
        'coverage': lambda: evidence.projection['coverage'].update(complete=False),
        'omitted': lambda: evidence.projection['omitted'].update(tasks=1),
        'requires-reconciliation': lambda: evidence.projection.update(requires_reconciliation=True),
    }
    changes[mutation]();evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and report['issues'] == ['projection_mismatch']


@pytest.mark.parametrize('case', ['read-error', 'delta-omitted', 'event-backlog', 'completion-omitted'])
def test_real_producer_unresolved_cases_remain_nonzero(evidence, case):
    import sqlite3
    if case == 'read-error':
        (evidence.home/'kanban.db').write_text('isolated unreadable database')
    elif case == 'delta-omitted':
        with sqlite3.connect(evidence.home/'kanban.db') as db:
            db.executemany("INSERT INTO tasks(id,status) VALUES(?, 'done')", [(str(i),) for i in range(40)])
    else:
        events = evidence.home/'workers/worker-a/events.jsonl'
        extra = ([{'kind': 'tool', 'data': 'x'*1000} for _ in range(100)] if case == 'event-backlog'
                 else [{'kind': 'turn_done', 'text': 'another result '+str(i)} for i in range(5)])
        with events.open('a') as stream:
            stream.write(''.join(json.dumps(v)+'\n' for v in extra))
    proc, projection = adapter_cli(evidence.home, evidence.artifact, '--checkpoint', evidence.checkpoint)
    assert proc.returncode == (2 if case == 'read-error' else 0)
    evidence.projection_path.write_text(json.dumps(projection))
    code, report = invoke(evidence)
    assert code == 3 and report['evidence_verified'] and report['issues']


def test_identity_precedes_artifact_json_and_checkpoint_reads(evidence, monkeypatch):
    from scripts import task_owner_verify as verifier
    evidence.artifact.write_text('{')
    evidence.projection['snapshot_artifact'].update(bytes=1, sha256='0'*64)
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    opened = []
    original = verifier.read_file
    def observe(path, *args, **kwargs):
        opened.append(Path(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(verifier, 'read_file', observe)
    report = verifier.verify(evidence.projection_path, evidence.checkpoint)
    assert report['issues'] == ['artifact_hash_mismatch']
    assert opened == [evidence.projection_path, evidence.artifact]


@pytest.mark.parametrize('alias', ['hardlink', 'symlink'])
def test_same_physical_snapshot_is_read_and_decoded_once(evidence, monkeypatch, alias):
    from scripts import task_owner_verify as verifier
    from scripts.task_owner_snapshot import project_observation
    candidate = evidence.home.parent/'same-consumed.json'
    if alias == 'hardlink':
        os.link(evidence.baseline, candidate)
    else:
        candidate.symlink_to(evidence.baseline)
    raw = evidence.baseline.read_bytes()
    ref = dict(path=str(candidate), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), observation_only=True)
    evidence.projection_path.write_text(json.dumps(project_observation(json.loads(raw), ref)))
    reads, decodes = [], []
    original_open, original_loads = verifier.os.fdopen, verifier.json.loads
    def observe_open(fd, *args, **kwargs):
        stream = original_open(fd, *args, **kwargs)
        class Reader:
            def __enter__(self): return self
            def __exit__(self, *args): stream.close()
            def fileno(self): return stream.fileno()
            def read(self, count):
                info = os.fstat(stream.fileno());reads.append((info.st_dev, info.st_ino))
                return stream.read(count)
        return Reader()
    def observe_loads(value, *args, **kwargs):
        if isinstance(value, str) and value.lstrip().startswith('{') and 'task-owner-observation/v1' in value:
            decodes.append(value)
        return original_loads(value, *args, **kwargs)
    monkeypatch.setattr(verifier.os, 'fdopen', observe_open)
    monkeypatch.setattr(verifier.json, 'loads', observe_loads)
    report = verifier.verify(evidence.projection_path, evidence.checkpoint)
    assert report['ready_for_processing'] and report['reference_relation'] == 'same_file'
    assert len(reads) == len(set(reads)) == 3 and len(decodes) == 1


def test_reader_failure_and_nonregular_input_are_explicit(evidence, monkeypatch):
    from scripts import task_owner_verify as verifier
    original = verifier.os.open
    def unavailable(path, *args, **kwargs):
        if Path(path) == evidence.artifact:
            raise PermissionError('isolated reader refusal')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(verifier.os, 'open', unavailable)
    report = verifier.verify(evidence.projection_path, evidence.checkpoint)
    assert not report['evidence_verified'] and report['issues'] == ['PermissionError']
    monkeypatch.setattr(verifier.os, 'open', original)
    evidence.projection['snapshot_artifact']['path'] = str(evidence.home)
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and not report['ready_for_processing']


def test_changed_input_and_oversize_input_are_not_truncated_to_success(evidence, monkeypatch):
    from scripts import task_owner_verify as verifier
    original = verifier.os.fstat
    calls = []
    def changed(fd):
        result = original(fd)
        if result.st_ino == evidence.artifact.stat().st_ino:
            calls.append(True)
            if len(calls) > 1:
                return SimpleNamespace(st_size=result.st_size, st_mtime_ns=result.st_mtime_ns+1, st_ctime_ns=result.st_ctime_ns)
        return result
    monkeypatch.setattr(verifier.os, 'fstat', changed)
    report = verifier.verify(evidence.projection_path, evidence.checkpoint)
    assert report['issues'] == ['input_changed']
    monkeypatch.setattr(verifier.os, 'fstat', original)
    with evidence.artifact.open('wb') as stream:
        stream.truncate(verifier.INPUT_BYTES+1)
    report = verifier.verify(evidence.projection_path, evidence.checkpoint)
    assert report['issues'] == ['input_too_large']


def test_v2_legacy_deadline_matches_existing_consumer_and_missing_health_stays_unknown(evidence):
    from scripts import task_owner_verify as verifier
    from scripts.task_owner_snapshot import load_previous
    old = json.loads(evidence.baseline.read_bytes());old.pop('next_review_at')
    evidence.baseline.write_text(json.dumps(old))
    expected = load_previous(checkpoint=evidence.checkpoint)['next_review_at']
    code, report = invoke(evidence)
    assert code == 0 and report['baseline_review_deadline'] == expected == 2800
    old.pop('errors');evidence.baseline.write_text(json.dumps(old))
    code, report = invoke(evidence)
    assert code == 3 and 'baseline_health_unknown' in report['issues']


def test_duplicate_json_and_truncated_artifact_are_rejected(evidence):
    ref = json.dumps(evidence.projection['snapshot_artifact'])
    evidence.projection_path.write_text('{"snapshot_artifact":'+ref+',"snapshot_artifact":'+ref+'}')
    code, report = invoke(evidence)
    assert code == 2 and report['issues'] == ['duplicate_json_key']
    evidence.artifact.write_text('{')
    evidence.projection['snapshot_artifact'].update(bytes=1, sha256=hashlib.sha256(b'{').hexdigest())
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and report['issues'] == ['JSONDecodeError']


def test_report_budget_is_explicit_and_never_becomes_consumption(evidence):
    from scripts import task_owner_verify as verifier
    report = verifier.verify(evidence.projection_path, evidence.checkpoint)
    report['snapshot_artifact']['path'] = '/'+'long-path'*5000
    raw = verifier.render(report)
    assert raw.isascii() and len(raw.encode())+1 <= 16384
    assert report['report_truncated'] and not report['ready_for_processing']
    assert report['consumption'] == report['native_ack'] == 'UNVERIFIED'


# Existing fake-home resolver/PM branches run real cron script/redaction/prompt code.
def test_actual_cron_cached_projection_is_verifiable_after_redaction(bundle, tmp_path, monkeypatch):
    from scripts import task_owner_verify as verifier
    from agent import redact
    worker_root = bundle.home/'workers/secret-worker';worker_root.mkdir(parents=True)
    (worker_root/'meta.json').write_text('{"id":"secret-worker"}')
    secret = 'sk-'+'x'*48
    (worker_root/'events.jsonl').write_text(json.dumps({'kind':'turn_done','text':secret+'\u2028\u0085'})+'\n')
    ok, stdout, provenance = bundle.fire()
    assert ok and secret not in stdout
    # Force mode must not query runtime config to reproduce the consumer's projection masking.
    monkeypatch.setattr(redact, '_redact_enabled', lambda: (_ for _ in ()).throw(AssertionError('no config reads')))
    cached = tmp_path/'cached.json';cached.write_text(stdout)
    report = verifier.verify(cached, bundle.checkpoint)
    assert report['ready_for_processing'] and report['projection_comparison'] == 'raw_or_cron_redacted'
    assert secret not in verifier.render(report)
    prompt = bundle.prompt((ok, stdout))
    assert bundle.value(stdout)['snapshot_artifact']['sha256'] in prompt
    assert provenance['collector_main'] == 1 and len(bundle.argvs) == 1


def test_artifact_reference_does_not_forward_arbitrary_content(evidence):
    evidence.projection['snapshot_artifact']['task_text'] = 'untrusted arbitrary content'
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and report['issues'] == ['invalid_artifact_reference']
    assert 'untrusted arbitrary content' not in json.dumps(report)


def test_projection_wake_numeric_is_not_the_native_false_gate(evidence):
    from cron.scheduler_prompt import _parse_wake_gate
    assert evidence.projection['wakeAgent'] is False
    assert _parse_wake_gate(json.dumps(evidence.projection)) is False
    evidence.projection['wakeAgent'] = 0
    assert _parse_wake_gate(json.dumps(evidence.projection)) is True
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and not report['ready_for_processing'], report


@pytest.mark.parametrize(('path', 'value'), [
    (('requires_reconciliation',), 0), (('coverage', 'complete'), 1),
    (('coverage', 'boards_read'), True), (('coverage', 'workers_observed'), True),
    (('omitted', 'tasks'), False), (('omitted_errors',), False),
])
def test_projection_boolean_and_count_shapes_are_distinct(evidence, path, value):
    target = evidence.projection
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 2 and report['issues'] == ['projection_mismatch']


def test_supplied_baseline_clock_error_is_not_healthy(evidence):
    from scripts.task_owner_snapshot import load_previous, collect
    old = json.loads(evidence.baseline.read_bytes()); old['observed_at'] = 'invalid'
    evidence.baseline.write_text(json.dumps(old))
    actual = collect(evidence.home, load_previous(checkpoint=evidence.checkpoint), now=1001)
    assert any(e['source'] == 'review_clock' for e in actual['errors'])
    code, report = invoke(evidence)
    assert code != 0 and not report['ready_for_processing'], report


@pytest.mark.parametrize('clock', [None, False])
def test_present_baseline_clock_is_a_finite_number(evidence, clock):
    old = json.loads(evidence.baseline.read_bytes()); old['observed_at'] = clock
    evidence.baseline.write_text(json.dumps(old))
    code, report = invoke(evidence)
    assert code == 2 and report['issues'] == ['invalid_baseline_clock']


@pytest.mark.parametrize('number_type', [int, float])
def test_numeric_projection_clocks_keep_existing_number_compatibility(evidence, number_type):
    for field in ('observed_at', 'next_review_at'):
        evidence.projection[field] = number_type(evidence.projection[field])
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    code, report = invoke(evidence)
    assert code == 0 and report['ready_for_processing']


@pytest.mark.parametrize('missing', [('observed_at',), ('observed_at', 'next_review_at')])
def test_absent_legacy_baseline_clock_keeps_v2_reference_fallback(evidence, missing):
    from scripts.task_owner_snapshot import load_previous
    old = json.loads(evidence.baseline.read_bytes())
    for field in missing:
        old.pop(field)
    evidence.baseline.write_text(json.dumps(old))
    deadline = load_previous(checkpoint=evidence.checkpoint)['next_review_at']
    code, report = invoke(evidence)
    assert code == 0 and report['baseline_review_deadline'] == deadline
    assert report['consumption'] == report['native_ack'] == 'UNVERIFIED' and not report['checkpoint_written']
