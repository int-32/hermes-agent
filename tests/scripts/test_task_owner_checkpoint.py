"""Owner-explicit checkpoint maintenance, real file identity and failure recovery."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import task_owner_checkpoint as checkpoint
from scripts import task_owner_verify as verifier
from tests.scripts.test_task_owner_verify import rewrite
from tests.scripts.test_task_owner_snapshot import adapter_cli

pytest_plugins = ['tests.scripts.test_task_owner_verify']
pytestmark = pytest.mark.platforms('posix')
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def case(evidence):
    events = evidence.home/'workers/worker-a/events.jsonl'
    with events.open('a') as stream:
        stream.write('{"kind":"turn_done","text":"new result"}\n')
    proc, evidence.projection = adapter_cli(evidence.home, evidence.artifact, '--checkpoint', evidence.checkpoint)
    assert proc.returncode == 0 and evidence.projection['wakeAgent'] is True
    evidence.projection_path.write_text(json.dumps(evidence.projection))
    record = json.loads(evidence.checkpoint.read_text())
    record.update(actual_actions=[{'evidence':'original-owner-record'}], owner_observations={'keep':True},
                  evidence_paths=['old-evidence'], failed_or_unverified={'unknown':'preserve'})
    record['consumed_fingerprints']['existing_owner_field'] = 'retain'
    evidence.checkpoint.write_text(json.dumps(record))
    evidence.expected = evidence.home.parent/'verified.json'
    evidence.expected.write_text(verifier.render(verifier.verify(evidence.projection_path, evidence.checkpoint)))
    evidence.before = evidence.home.parent/'before.json'
    evidence.original = evidence.checkpoint.read_bytes()
    return evidence


def call(case, action='commit'):
    return checkpoint.maintain(action, case.projection_path, case.checkpoint, case.expected, case.before)


def test_explicit_commit_preserves_v2_and_owner_records_and_reads_back(case):
    old = json.loads(case.original)
    artifact = case.artifact.read_bytes(); baseline = case.baseline.read_bytes()
    result = call(case)
    assert result['state'] == 'COMMITTED' and result['exit_code'] == 0
    assert result['consumption'] == result['native_ack'] == 'UNVERIFIED'
    assert result['checkpoint_written'] is True
    new = json.loads(case.checkpoint.read_bytes())
    assert new['consumed_fingerprints']['snapshot_path'] == str(case.artifact)
    assert datetime.fromisoformat(new['observed_at']).tzinfo is not None
    new['consumed_fingerprints']['snapshot_path'] = old['consumed_fingerprints']['snapshot_path']
    new['observed_at'] = old['observed_at']
    assert new == old
    assert case.before.read_bytes() == case.original
    assert case.artifact.read_bytes() == artifact and case.baseline.read_bytes() == baseline
    assert call(case, 'reconcile')['state'] == 'COMMITTED'
    assert call(case)['state'] == 'COMMITTED'  # readback, no second publish


@pytest.mark.parametrize('defect', ['hash', 'type', 'unknown', 'omitted', 'backlog', 'false'])
def test_unready_or_skipped_observation_never_writes_checkpoint(case, defect):
    if defect == 'hash':
        case.projection['snapshot_artifact']['sha256'] = '0'*64
        case.projection_path.write_text(json.dumps(case.projection))
    elif defect == 'type':
        case.projection['wakeAgent'] = 0
        case.projection_path.write_text(json.dumps(case.projection))
    else:
        def mutate(full):
            if defect == 'unknown':full['errors'].append(dict(source='missing.db', error='OSError', reason='previous_board_unavailable'))
            elif defect == 'omitted':full['delta']['removed_executors'] = [str(i) for i in range(30)]
            elif defect == 'backlog':full['executors'][0]['has_more'] = True
            else:full.update(wakeAgent=False, reasons=[])
        rewrite(case, mutate)
        case.expected.write_text(verifier.render(verifier.verify(case.projection_path, case.checkpoint)))
    result = call(case)
    assert result['exit_code'] != 0 and result['state'] != 'COMMITTED'
    assert case.checkpoint.read_bytes() == case.original and not case.before.exists()


@pytest.mark.parametrize('changed', ['checkpoint', 'baseline'])
def test_expected_identity_conflict_is_not_overwritten(case, changed):
    path = getattr(case, changed)
    path.write_bytes(path.read_bytes()+b' ')
    current = case.checkpoint.read_bytes()
    result = call(case)
    assert result['state'] == 'CONFLICT' and result['exit_code'] == 4
    assert case.checkpoint.read_bytes() == current and not case.before.exists()


@pytest.mark.parametrize('kind', ['direct', 'symlink', 'hardlink', 'parent-symlink'])
def test_before_artifact_cannot_alias_any_input(case, kind):
    original = case.before
    if kind == 'direct':case.before = case.artifact
    elif kind == 'symlink':case.before.symlink_to(case.baseline)
    elif kind == 'hardlink':os.link(case.checkpoint, case.before)
    else:
        link = case.home.parent/'linked'; link.symlink_to(case.artifact.parent, target_is_directory=True)
        case.before = link/case.artifact.name
    protected = {p: p.read_bytes() for p in (case.artifact, case.baseline, case.checkpoint, case.expected, case.projection_path)}
    result = call(case)
    assert result['exit_code'] != 0 and result['state'] != 'COMMITTED'
    assert all(p.read_bytes() == raw for p,raw in protected.items())
    if original != case.before:assert not original.exists()


@pytest.mark.parametrize('alias', ['artifact-baseline', 'checkpoint-baseline', 'checkpoint-artifact'])
def test_data_aliases_are_not_a_consumption_commit(case, alias):
    if alias == 'artifact-baseline':
        case.artifact.unlink();os.link(case.baseline, case.artifact)
    else:
        case.checkpoint.unlink();os.link(case.baseline if alias.endswith('baseline') else case.artifact, case.checkpoint)
    current = case.checkpoint.read_bytes()
    result = call(case)
    assert result['exit_code'] != 0
    assert case.checkpoint.read_bytes() == current


def test_canonical_checkpoint_symlink_is_supported_and_mode_preserved(case):
    actual = case.checkpoint.with_name('actual.json');case.checkpoint.rename(actual);case.checkpoint.symlink_to(actual)
    actual.chmod(0o640)
    result = call(case)
    assert result['state'] == 'COMMITTED' and case.checkpoint.is_symlink()
    assert actual.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('phase', ['before-write', 'file-fsync', 'replace', 'after-replace', 'directory'])
def test_actual_helper_failures_reconcile_without_blind_retry(case, monkeypatch, phase):
    import utils
    original = utils.atomic_write_text
    def before_write():
        def fail(*a, **kw):raise OSError('fixture before write')
        monkeypatch.setattr(utils, 'atomic_write_text', fail)
    def file_fsync():
        real_fsync = utils.os.fsync
        calls = []
        def fail_checkpoint_file(fd):
            calls.append(fd)
            if len(calls) == 2:raise OSError('fixture checkpoint file fsync')
            return real_fsync(fd)
        monkeypatch.setattr(utils.os, 'fsync', fail_checkpoint_file)
    def replace():
        monkeypatch.setattr(utils.os, 'replace', lambda *a: (_ for _ in ()).throw(PermissionError('fixture replace')))
    def after_replace():
        def lost(*a, **kw):
            original(*a, **kw)
            raise OSError('fixture lost reply')
        monkeypatch.setattr(utils, 'atomic_write_text', lost)
    def directory():
        monkeypatch.setattr(utils, 'fsync_directory', lambda path: (_ for _ in ()).throw(OSError('fixture directory')))
    {'before-write':before_write,'file-fsync':file_fsync,'replace':replace,
     'after-replace':after_replace,'directory':directory}[phase]()
    result = call(case)
    expected = 'COMMITTED' if phase in ('after-replace', 'directory') else 'NOT_COMMITTED'
    assert result['state'] == expected
    assert result['exit_code'] == (0 if expected == 'COMMITTED' else 3)
    assert case.before.read_bytes() == case.original
    if expected == 'NOT_COMMITTED':assert case.checkpoint.read_bytes() == case.original
    monkeypatch.undo()
    assert call(case, 'reconcile')['state'] == expected
    assert call(case)['state'] == 'COMMITTED'


def test_unknown_and_reconstruction_do_not_overwrite(case):
    assert call(case)['state'] == 'COMMITTED'
    case.checkpoint.write_text('{partial')
    current = case.checkpoint.read_bytes()
    assert call(case, 'reconcile')['state'] == 'UNKNOWN'
    assert call(case)['exit_code'] == 4 and case.checkpoint.read_bytes() == current


def test_response_loss_then_fresh_cli_reconciles_same_exact_target(case):
    result = call(case)
    args = [sys.executable, str(ROOT/'scripts/task_owner_checkpoint.py'), 'reconcile',
        '--projection', str(case.projection_path), '--checkpoint', str(case.checkpoint),
        '--expected', str(case.expected), '--before', str(case.before)]
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result['state'] == 'COMMITTED' and proc.returncode == 0
    report = json.loads(proc.stdout)
    assert report['state'] == 'COMMITTED' and report['checkpoint_written'] is False
    assert report['native_ack'] == 'UNVERIFIED'


def test_two_real_supported_cli_writers_do_not_lose_update(case):
    other = case.home.parent/'other-projection.json'
    value = json.loads(case.projection_path.read_text());full = json.loads(case.artifact.read_text())
    full['observed_at'] += 1;full['next_review_at'] += 1
    second = case.artifact.with_name('second.json');raw = (json.dumps(full)+'\n').encode();second.write_bytes(raw)
    value['snapshot_artifact'] = dict(path=str(second), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), observation_only=True)
    from scripts.task_owner_snapshot import project_observation
    other.write_text(json.dumps(project_observation(full, value['snapshot_artifact'])))
    expected = other.with_name('other-expected.json');expected.write_text(verifier.render(verifier.verify(other, case.checkpoint)))
    def run(projection, report, before):
        return subprocess.run([sys.executable,str(ROOT/'scripts/task_owner_checkpoint.py'),'commit',
            '--projection',str(projection),'--checkpoint',str(case.checkpoint),'--expected',str(report),
            '--before',str(before)],cwd=ROOT,capture_output=True,text=True,check=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run,case.projection_path,case.expected,case.before)
        second_call = pool.submit(run,other,expected,other.with_name('other-before.json'))
        results = [first.result(),second_call.result()]
    assert sorted(p.returncode for p in results) == [0,4]
    winner = next(json.loads(p.stdout) for p in results if p.returncode == 0)
    assert winner['state'] == 'COMMITTED'
    assert json.loads(case.checkpoint.read_text())['consumed_fingerprints']['snapshot_path'] in (str(case.artifact),str(second))


@pytest.mark.parametrize('changed', ['checkpoint', 'baseline', 'artifact', 'projection_path'])
def test_every_pinned_input_rechecked_at_publication(case, monkeypatch, changed):
    real_save = checkpoint.save_before
    target = getattr(case, changed)
    def changed_after_verification(path, raw):
        real_save(path, raw)
        target.write_bytes(target.read_bytes()+b' ')
    monkeypatch.setattr(checkpoint, 'save_before', changed_after_verification)
    result = call(case)
    assert result['state'] == 'CONFLICT' and result['exit_code'] == 4
    assert case.checkpoint.read_bytes() == case.original+(b' ' if changed == 'checkpoint' else b'')


@pytest.mark.parametrize('fault', ['EXDEV-success', 'fallback-partial', 'readback-error', 'dir-open-best-effort'])
def test_existing_helper_fallback_and_unknown_readback_are_not_automatic_success(case, monkeypatch, fault):
    import errno
    import utils
    if fault.startswith('EXDEV') or fault == 'fallback-partial':
        def exdev(*a):raise OSError(errno.EXDEV, 'fixture cross-device')
        monkeypatch.setattr(utils.os, 'replace', exdev)
        if fault == 'fallback-partial':
            def partial(tmp, target):
                Path(target).write_bytes(b'{partial')
                raise OSError('fixture interrupted fallback')
            monkeypatch.setattr(utils, '_copy_fallback', partial)
    elif fault == 'readback-error':
        real_write = utils.atomic_write_text
        real_read = verifier.read_file
        def write_then_deny(*a, **kw):
            real_write(*a, **kw)
            def deny(path, cache, *a, **kw):
                if Path(path) == case.checkpoint:raise PermissionError('fixture readback')
                return real_read(path, cache, *a, **kw)
            monkeypatch.setattr(verifier, 'read_file', deny)
        monkeypatch.setattr(utils, 'atomic_write_text', write_then_deny)
    else:
        real_open = utils.os.open
        def deny_directory(path, flags, *a, **kw):
            if flags & getattr(os,'O_DIRECTORY',0):raise PermissionError('fixture dir fsync open')
            return real_open(path,flags,*a,**kw)
        monkeypatch.setattr(utils.os,'open',deny_directory)
    result = call(case)
    expected = 'UNKNOWN' if fault in ('fallback-partial','readback-error') else 'COMMITTED'
    assert result['state'] == expected and result['exit_code'] == (4 if expected == 'UNKNOWN' else 0)
    assert case.before.read_bytes() == case.original
    assert result['directory_durability'] == 'BEST_EFFORT_NOT_POWER_LOSS_PROOF'
    monkeypatch.undo()
    assert call(case,'reconcile')['state'] == ('UNKNOWN' if fault == 'fallback-partial' else 'COMMITTED')


def test_mechanical_verify_reuses_full_observation_read_and_decode(case, monkeypatch):
    reads = []
    original = verifier.read_file
    def record(path, cache, *a, **kw):
        before = set(cache)
        result = original(path, cache, *a, **kw)
        if set(cache) != before:reads.append(Path(path))
        return result
    monkeypatch.setattr(verifier,'read_file',record)
    assert call(case)['state'] == 'COMMITTED'
    assert reads.count(case.artifact) == reads.count(case.baseline) == 1


@pytest.mark.parametrize('missing', ['artifact', 'baseline', 'checkpoint', 'expected'])
def test_missing_fixed_input_is_nonzero_without_writing(case, missing):
    getattr(case, missing).unlink()
    result = call(case)
    assert result['exit_code'] != 0 and result['state'] != 'COMMITTED'
    assert not case.before.exists()
    if missing != 'checkpoint':assert case.checkpoint.read_bytes() == case.original


def test_self_reported_processing_cannot_override_unresolved_evidence(case):
    rewrite(case, lambda full: full['errors'].append(dict(source='missing',error='OSError',reason='read_error')))
    fake = verifier.verify(case.projection_path, case.checkpoint)
    fake.update(processed=True,ready_for_processing=True,evidence_verified=True)
    case.expected.write_text(json.dumps(fake))
    result = call(case)
    assert result['state'] == 'NOT_READY' and result['exit_code'] == 3
    assert case.checkpoint.read_bytes() == case.original and not case.before.exists()


def test_recovery_evidence_mismatch_cannot_become_a_new_checkpoint(case):
    assert call(case)['state'] == 'COMMITTED'
    current = case.checkpoint.read_bytes()
    case.before.write_bytes(case.before.read_bytes()+b' ')
    assert call(case,'reconcile')['state'] == 'CONFLICT'
    assert call(case)['exit_code'] == 4 and case.checkpoint.read_bytes() == current


def test_bounded_report_retains_actual_commit_state_without_ack(case):
    result = call(case)
    result['checkpoint']['path'] = '/'+'long'*5000
    raw = checkpoint.render(result)
    value = json.loads(raw)
    assert raw.isascii() and len(raw)+1 <= 16384
    assert value['report_truncated'] and value['exit_code'] != 0
    assert value['state'] == 'COMMITTED' and value['native_ack'] == 'UNVERIFIED'
