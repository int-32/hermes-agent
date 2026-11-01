"""Owner-explicit v2 file maintenance, never business acceptance or native ACK.

Only participating writers share the canonical sidecar lock. Drain legacy writers
before migration. Recovery uses one exact before-K artifact, not a task ledger.
"""
import argparse
import copy
from datetime import datetime, UTC
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import task_owner_verify as verifier
from scripts.task_owner_snapshot import encoded, snapshot_destination, PROJECTION_BYTES
from hermes_cli.active_sessions import _FileLock


class Conflict(ValueError):
    """The chosen K/baseline/projection is no longer the expected input."""


def identity(value):
    verifier.fields(value, ('path', 'bytes', 'sha256'))
    verifier.require(isinstance(value['path'], str) and Path(value['path']).is_absolute()
                     and verifier.integer(value['bytes']) and verifier.sha(value['sha256']), 'invalid_expected_identity')
    return value


def matches(actual, expected, *, path=True):
    return (actual['bytes'] == expected['bytes'] and actual['sha256'] == expected['sha256']
            and (not path or Path(actual['path']).resolve() == Path(expected['path']).resolve()))


def expected_inputs(path, cache):
    entry, _ = verifier.read_file(path, cache, PROJECTION_BYTES)
    expected = verifier.decode(entry)
    for field in ('checkpoint', 'consumed_snapshot', 'snapshot_artifact', 'projection_input'):
        identity(expected[field])
    return expected


def destinations(checkpoint, before, projection, expected_path, expected):
    paths = {checkpoint, projection, expected_path,
             Path(expected['snapshot_artifact']['path']), Path(expected['consumed_snapshot']['path'])}
    canonical = checkpoint.resolve()
    if canonical.exists() and canonical.stat().st_nlink != 1:
        raise Conflict('checkpoint_hardlink_lock_namespace')
    before = snapshot_destination(before, paths)
    snapshot_destination(canonical, paths - {checkpoint} | {before})
    snapshot_destination(Path(expected['snapshot_artifact']['path']),
                         {checkpoint, Path(expected['consumed_snapshot']['path'])})
    lock = snapshot_destination(canonical.with_name('.' + canonical.name + '.lock'), paths | {before})
    verifier.require(before.parent.is_dir(), 'before_parent_missing')
    return canonical, before, lock


def checked_observation(projection, old_path, expected, cache):
    report = verifier.verify(projection, old_path, cache=cache)
    # Compare every identity that the verifier managed to read, before classifying
    # health. A changed valid baseline is a conflict even if it is now unhealthy.
    for field in ('checkpoint', 'consumed_snapshot', 'snapshot_artifact', 'projection_input'):
        if field in report and not matches(report[field], expected[field], path=field != 'checkpoint'):
            raise Conflict(field + '_changed')
    if not report['evidence_verified'] or not report['ready_for_processing']:
        return report, None, None
    old, _ = verifier.read_file(old_path, cache)
    full, _ = verifier.read_file(report['snapshot_artifact']['path'], cache)
    return report, old, verifier.decode(full)


def target_bytes(old, full, artifact):
    record = copy.deepcopy(verifier.decode(old))
    record['consumed_fingerprints']['snapshot_path'] = artifact['path']
    record['observed_at'] = datetime.fromtimestamp(full['observed_at'], UTC).isoformat()
    return (encoded(record, ensure_ascii=True) + '\n').encode('ascii')


def readback(checkpoint, old, target):
    try:
        current, _ = verifier.read_file(checkpoint, {})
    except (OSError, ValueError, RuntimeError):
        return 'UNKNOWN'
    if current['raw'] == target:
        return 'COMMITTED'
    return 'NOT_COMMITTED' if current['raw'] == old else 'UNKNOWN'


def save_before(path, raw):
    # Exclusive creation: never replace a prior recovery artifact or an input alias.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def fresh_preconditions(checkpoint, expected, cache):
    # Reopen/fstat every pinned input at publication. The verifier's call-local
    # cache rejects changed stamps and avoids another full O/baseline read.
    for field in ('checkpoint', 'consumed_snapshot', 'snapshot_artifact', 'projection_input'):
        path = checkpoint if field == 'checkpoint' else Path(expected[field]['path'])
        try:
            _, facts = verifier.read_file(path, cache)
        except verifier.InvalidEvidence as exc:
            raise Conflict(field + '_changed_before_publication') from exc
        if not matches(facts, expected[field]):
            raise Conflict(field + '_changed_before_publication')


def publish(checkpoint, before, old, target, expected, cache, result):
    import utils
    try:
        if not before.exists():
            save_before(before, old)
        fresh_preconditions(checkpoint, expected, cache)
        utils.atomic_write_text(checkpoint, target.decode('ascii'), preserve_mode=True, fsync_dir=True)
        result['checkpoint_written'] = True
    except Conflict:
        raise
    except (OSError, RuntimeError) as exc:
        result['issues'].append(type(exc).__name__)
    # An exception after replace is not proof of failure; an unreturned success is
    # reconstructed from the SAME before-K and deterministic target, never replayed.
    result['state'] = readback(checkpoint, old, target)
    if result['state'] == 'COMMITTED':
        result['checkpoint_written'] = True
    return result


def locked_maintenance(action, projection, checkpoint, expected, before, cache, result):
    exists = before.exists()
    if action == 'reconcile' and not exists:
        result.update(state='UNKNOWN', issues=['before_evidence_missing'])
        return result
    old_path = before if exists else checkpoint
    report, old, full = checked_observation(projection, old_path, expected, cache)
    result.update(verification=report['status'], issues=report['issues'])
    if old is None:
        result['state'] = 'NOT_READY' if report['evidence_verified'] else 'INVALID'
        return result
    if not full['wakeAgent']:
        result.update(state='SKIPPED', issues=['false_gate_never_promotes'])
        return result
    target = target_bytes(old, full, expected['snapshot_artifact'])
    result.update(snapshot_artifact=expected['snapshot_artifact'], checkpoint=expected['checkpoint'],
                  before=dict(path=str(before), bytes=len(old['raw']), sha256=old['sha256']))
    state = readback(checkpoint, old['raw'], target)
    if action == 'reconcile' or state == 'COMMITTED':
        result['state'] = state
        return result
    if state != 'NOT_COMMITTED':
        result.update(state='UNKNOWN', issues=['checkpoint_differs_from_before_and_target'])
        return result
    return publish(checkpoint, before, old['raw'], target, expected, cache, result)


def maintain(action, projection, checkpoint, expected, before):
    result = dict(state='INVALID', issues=[], checkpoint_written=False,
                  consumption='UNVERIFIED', native_ack='UNVERIFIED',
                  collection_baseline_binding='UNVERIFIED_NOT_IN_OBSERVATION_FORMAT',
                  directory_durability='BEST_EFFORT_NOT_POWER_LOSS_PROOF',
                  writer_boundary='cooperating_canonical_sidecar_lock_only')
    try:
        verifier.require(action in ('commit', 'reconcile'), 'invalid_action')
        paths = [Path(p) for p in (projection, checkpoint, expected, before)]
        verifier.require(all(p.is_absolute() for p in paths), 'input_path_not_absolute')
        projection, checkpoint, expected_path, before = paths
        cache = {}
        expected = expected_inputs(expected_path, cache)
        if checkpoint.resolve() != Path(expected['checkpoint']['path']).resolve():
            raise Conflict('checkpoint_path_changed')
        checkpoint, before, lock = destinations(checkpoint, before, projection, expected_path, expected)
        with _FileLock(lock):
            result = locked_maintenance(action, projection, checkpoint, expected, before, cache, result)
    except Conflict as exc:
        result.update(state='CONFLICT', issues=[str(exc)])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RuntimeError) as exc:
        result.update(state='INVALID', issues=[str(exc) if isinstance(exc, verifier.InvalidEvidence) else type(exc).__name__])
    result['exit_code'] = {'COMMITTED': 0, 'INVALID': 2, 'NOT_READY': 3, 'SKIPPED': 3,
                           'NOT_COMMITTED': 3, 'UNKNOWN': 4, 'CONFLICT': 4}[result['state']]
    return result


def render(result):
    raw = encoded(result, ensure_ascii=True)
    if len(raw) + 1 > PROJECTION_BYTES:
        result = {key: value for key, value in result.items() if key not in ('snapshot_artifact', 'checkpoint', 'before')}
        result.update(report_truncated=True, exit_code=4)
        raw = encoded(result, ensure_ascii=True)
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('commit', 'reconcile'))
    for key in ('projection', 'checkpoint', 'expected', 'before'):
        parser.add_argument('--'+key, type=Path, required=True)
    args = parser.parse_args()
    report = maintain(args.action, args.projection, args.checkpoint, args.expected, args.before)
    raw = render(report)
    print(raw)
    return json.loads(raw)['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
