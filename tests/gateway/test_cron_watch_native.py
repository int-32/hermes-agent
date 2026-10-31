"""Real bootstrap/cron/plugin IPC/native tools/private return, with inert transport/model."""
import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.platforms("posix")

from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home
from tests.gateway.fixtures.local_recovery_probe import child_env, daemon
from tests.waiting import async_wait_until


PLUGIN = '''
import json, os
from pathlib import Path

def register(ctx):
    home = Path(os.environ["HERMES_HOME"])
    content = "Check watch-task; read " + str(home / "documents/project/watch-task/HANDOFF.md")
    def inspect(**kwargs):
        fire = ctx.current_cron_execution()
        if fire is None:
            return
        if (home / "observe-restart").exists():
            original = json.loads((home / "producer.json").read_text())
            result = json.loads(ctx.dispatch_tool("cron_task_check", {"content": content,
                "execution_id": original["execution_id"]}))
            (home / "observed.json").write_text(json.dumps(result))
            return
        try:
            ctx.dispatch_tool("cron_task_check", {"content": content})
        except TimeoutError:
            lost_reply = True
        else:
            lost_reply = False
        replies = [json.loads(ctx.dispatch_tool("cron_task_check", {"content": content})) for _ in range(2)]
        conflict = json.loads(ctx.dispatch_tool("cron_task_check", {"content": content + " changed"}))
        (home / "producer.json").write_text(json.dumps({
            "execution_id": fire.execution_id, "job_id": fire.job_id, "replies": replies,
            "conflict": conflict, "lost_reply": lost_reply}))
    def status(args, **kwargs):
        fire = json.loads((home / "producer.json").read_text())
        result = json.loads(ctx.dispatch_tool("cron_task_check", {"content": content, "execution_id": fire["execution_id"]}))
        (home / "observed.json").write_text(json.dumps(result))
        return json.dumps(result)
    ctx.register_hook("pre_tool_call", inspect)
    ctx.register_tool(name="watch_status", toolset="watch-probe", handler=status,
        schema={"name":"watch_status", "description":"Observe the fixture receipt", "parameters":{"type":"object","properties":{}}})
'''


def cold(name, args):
    return ToolCall('tool_call', {'name': name, 'arguments': args})


def responder(home, held=None, release=None, owner_loss=False):
    counts = {}
    doc = str(home / 'documents/project/watch-task/HANDOFF.md')
    def answer(body):
        messages = body['body']['messages']
        text = str(next(m['content'] for m in reversed(messages) if m['role'] == 'user'))
        phase = counts.get(text, 0)
        counts[text] = phase + 1
        if text == 'HOLD_MAIN':
            held.set()
            if not release.wait(40):
                raise RuntimeError('fixture model barrier was not released')
            return Text('released')
        if 'WARM_' in text:
            return Text('warm')
        if 'QUERY_STATUS' in text:
            return cold('watch_status', {}) if phase == 0 else Text('status observed')
        if 'Script Error' in text:
            return Text('[CRON_FAILURE] deterministic collection failed')
        if 'read the fixture note' in text:
            return ToolCall('read_file', {'path': str(home / 'note.txt')}) if phase == 0 else Text('producer queued')
        if 'Scheduled task check;' in text:
            if owner_loss and phase == 0:
                held.set()
                if not release.wait(40):
                    raise RuntimeError('owner-loss model barrier was not released')
            return [ToolCall('read_file', {'path': doc}), cold('coordinate_session', {
                'request_id': 'fixture-watch-root', 'task_id': 'watch-task', 'handoff_path': doc,
                'target': 'task:watch-task'}), cold('coordinate_session', {
                'request_id': 'duplicate-watch-root', 'task_id': 'watch-task', 'handoff_path': doc,
                'target': 'task:watch-task'}), Text('queued')][min(phase, 3)]
        if 'Native document handoff;' in text:
            return [ToolCall('read_file', {'path': doc}), cold('coordinate_session', {
                'request_id': 'ancestor-loop', 'task_id': 'watch-task', 'handoff_path': doc, 'target': 'main'}),
                cold('coordinate_session', {'action': 'complete', 'request_id': 'fixture-watch-root', 'outcome': 'completed'}),
                cold('coordinate_session', {'request_id': 'after-completion', 'task_id': 'watch-task',
                    'handoff_path': doc, 'target': 'main'}), Text('private verified')][min(phase, 4)]
        if 'Native document dependency receipt;' in text:
            return [ToolCall('read_file', {'path': doc}), cold('coordinate_session', {
                'action': 'complete', 'request_id': 'fixture-watch-root', 'outcome': 'completed'}),
                cold('coordinate_session', {'request_id':'after-completion', 'task_id':'watch-task',
                    'handoff_path': doc, 'target':'main' if 'handoff;' in text else 'task:watch-task'}),
                Text('private verified')][min(phase, 3)]
        return Text('fixture idle')
    return answer


def rows(home, table):
    with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM ' + table)]


def private_work(home):
    path = home / 'runtime/agent_handoff.db'
    if not path.exists():
        return []
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT record FROM requests')]


@pytest.mark.parametrize('scenario', ['complete-main-reset', 'complete-task-reset', 'complete-busy', 'owner-loss', 'consent-false', 'unbound-job', 'route-mismatch',
                                     'no-change', 'collection-error'])
def test_canonical_watch_reaches_native_task_and_correlated_private_return(tmp_path, scenario):
    from hermes_time import now

    root = Path(__file__).resolve().parents[2]
    user = tmp_path / 'user'
    home = tmp_path / 'home'
    user.mkdir()
    plugin = home / 'plugins/watch-probe'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: watch-probe\nversion: 0.0.1\n')
    (plugin / '__init__.py').write_text(PLUGIN)
    runtime = Path('/tmp') / ('hwe-' + hashlib.sha256(str(root).encode()).hexdigest()[:12])  # no-tmp: ok — official AF_UNIX socket length limit
    runtime.mkdir(mode=0o700, exist_ok=True)
    held, release = threading.Event(), threading.Event()
    with FakeLLMServer(responder(home, held, release, scenario == 'owner-loss')) as model:
        config = '''
compression:
  in_place: false
  protect_first_n: 1
  protect_last_n: 2
gateway:
  multiplex_profiles: true
  profile_routes:
    - name: peer-main
      platform: discord
      chat_id: '101'
      profile: peer
platforms:
  discord:
    enabled: true
    token: fixture-only
    typing_indicator: false
platform_toolsets:
  discord: [file, agent_handoff, watch-probe]
plugins:
  isolation: host
  enabled: [watch-probe, cron-task-check]
  entries:
    watch-probe:
      allow_gateway_injection: true
    cron-task-check:
      allow_gateway_injection: true
      settings:
        watch_jobs:
          watch-fixture: watch-task
'''
        if scenario == 'consent-false':
            config = config.replace('cron-task-check:\n      allow_gateway_injection: true',
                                    'cron-task-check:\n      allow_gateway_injection: false')
        if scenario == 'unbound-job':
            config = config.replace('watch-fixture: watch-task', 'another-job: watch-task')
        write_hermes_home(home, model.base_url, extra_config=config)
        (home / 'scenario').write_text(scenario)
        with (home / '.env').open('a') as out:
            out.write('DISCORD_ALLOWED_USERS=42\nDISCORD_BOT_TOKEN=fixture-only\n')
        (home / 'note.txt').write_text('isolated delta')
        env = child_env()
        env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
            HERMES_KANBAN_HOME=str(home), HERMES_RUNTIME_DIR=str(tmp_path / 'empty-pm'),
            XDG_RUNTIME_DIR=str(runtime), PYTHONPATH=str(root), PYTHONUNBUFFERED='1')

        async def run():
            await async_wait_until(lambda: (home / 'native-warm').exists() or (home / 'fixture-error').exists(), timeout=45)
            assert not (home / 'fixture-error').exists(), (home / 'fixture-error').read_text()
            assert json.loads((home / 'peer-proof.json').read_text())['source']['profile'] == 'peer'
            await async_wait_until(lambda: len([r for r in rows(home, 'session_admissions')
                if r['request_id'] in {'warm-main', 'warm-task'} and r['status'] == 'terminal']) == 2, timeout=45)
            baseline = len(model.main_requests())
            constructors = len((home / 'constructors.jsonl').read_text().splitlines())
            if scenario == 'complete-busy':
                (home / 'hold-main').touch()
                await async_wait_until(held.is_set, timeout=25)
            (home / 'cron').mkdir(exist_ok=True)
            job = {
                'id': 'watch-fixture', 'name': 'watch fixture', 'prompt': 'read the fixture note',
                'schedule': {'kind': 'interval', 'minutes': 240},
                'next_run_at': (now() - timedelta(minutes=1)).isoformat(),
                'enabled': True, 'state': 'scheduled', 'deliver': 'local',
                'repeat': {'times': None, 'completed': 0}}
            if scenario in {'no-change', 'collection-error'}:
                script = home / 'scripts/collect.py'
                script.parent.mkdir(exist_ok=True)
                script.write_text('print(\'{"wakeAgent": false}\')' if scenario == 'no-change' else
                                  'print("deterministic collection failed"); raise SystemExit(7)')
                job.update(script=str(script), interpreter=sys.executable)
            (home / 'cron/jobs.json').write_text(json.dumps({'jobs': [job]}))
            tick = await asyncio.to_thread(subprocess.run,
                [sys.executable, '-c', 'from cron.scheduler import tick; tick(verbose=False, sync=True)'],
                cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
            assert tick.returncode == 0, tick.stdout + tick.stderr
            if scenario in {'no-change', 'collection-error'}:
                await async_wait_until(lambda: any(r['principal_id'] == 'cron-owner' and r['status'] == 'terminal'
                    for r in rows(home, 'session_admissions')), timeout=35)
                assert not watch_rows(home) and not private_work(home)
                assert not (home / 'producer.json').exists()
                if scenario == 'no-change':
                    assert len(model.main_requests()) == baseline
                    assert len((home / 'constructors.jsonl').read_text().splitlines()) == constructors
                else:
                    assert len(model.main_requests()) > baseline
                    assert 'Script Error' in json.dumps(model.main_requests()[baseline:])
                save_evidence(root, tmp_path, scenario=scenario, wake_admissions=watch_rows(home),
                    constructors_before=constructors, constructors_after=(home / 'constructors.jsonl').read_text(),
                    model_before=baseline, requests=model.main_requests())
                return
            await async_wait_until(lambda: (home / 'producer.json').exists() or any(
                r['principal_id'] == 'cron-owner' and r['status'] == 'terminal'
                for r in rows(home, 'session_admissions')), timeout=45)
            assert (home / 'producer.json').exists(), dict(admissions=rows(home, 'session_admissions'),
                model=model.main_requests(), tick=tick.stdout + tick.stderr)
            seen = json.loads((home / 'producer.json').read_text())
            if scenario == 'owner-loss':
                await async_wait_until(held.is_set, timeout=25)
                assert len(watch_rows(home)) == 1 and watch_rows(home)[0]['status'] == 'started'
                assert seen['lost_reply'] is True
                assert len({r['admission_id'] for r in seen['replies']}) == 1
                assert not private_work(home)
                return
            if not scenario.startswith('complete'):
                expected = {'consent-false': 'consent required', 'unbound-job': 'no configured task binding',
                            'route-mismatch': 'binding changed'}[scenario]
                assert all(expected in r.get('error', '') and r['completed'] is False for r in seen['replies']), seen
                assert not watch_rows(home) and not private_work(home)
                save_evidence(root, tmp_path, scenario=scenario, producer=seen, wake_admissions=watch_rows(home))
                return
            assert seen["lost_reply"] is True, seen
            assert 'admission_conflict' in seen['conflict']['error'], seen
            assert len({r['admission_id'] for r in seen['replies']}) == 1, seen
            assert all(not r['completed'] for r in seen['replies']), seen
            if scenario == 'complete-busy':
                assert all(r['status'] == 'queued' for r in seen['replies']), seen
                hold = next(r for r in rows(home, 'session_admissions') if r['request_id'] == 'hold-main')
                assert watch_rows(home)[0]['seq'] > hold['seq'] and hold['status'] == 'started'
                release.set()
            await async_wait_until(lambda: any(r['status'] == 'terminal'
                for r in watch_rows(home)), timeout=30)
            work = private_work(home)
            assert len(work) == 1, dict(private=work, model=model.main_requests())
            await async_wait_until(lambda: private_work(home)[0]['status'] in
                {'delivered', 'blocked', 'failed', 'ambiguous'}, timeout=35)
            work = private_work(home)
            assert work[0]['status'] == 'delivered', work
            record = work[0]
            assert record['producer_admission_id'] == seen['replies'][0]['admission_id'], record
            assert record['outcome'] == 'completed' and record['continuation_confirmed'], record
            assert {a['phase'] for a in record['acknowledgements']} == {'origin', 'request'}, record
            assert len([r for r in rows(home, 'session_admissions') if r['request_id'].startswith('["cron-task-watch"')]) == 1
            (home / 'query-status').touch()
            await async_wait_until(lambda: (home / 'observed.json').exists(), timeout=25)
            observed = json.loads((home / 'observed.json').read_text())
            assert observed['completed'] is True and observed['model_completed'] is True, observed
            assert observed['first_tool'] == 'read_file', observed
            assert observed['private_requests'][0].get('request_first_tool', {}).get('name') == 'read_file', observed
            assert observed['private_requests'][0].get('origin_first_tool', {}).get('name') == 'read_file', observed
            assert observed['admission_id'] == record['producer_admission_id'], observed
            tool_results = [m['content'] for b in model.main_requests() for m in b['messages'] if m['role'] == 'tool']
            assert any('recursive coordination loop refused' in text for text in tool_results), tool_results
            assert any('dispatch after completion' in text for text in tool_results), tool_results
            await verify_continuity(home, seen['replies'][0]['admission_id'],
                                    'task' if scenario == 'complete-task-reset' else 'main')
            save_evidence(root, tmp_path, scenario=scenario, producer=seen, private=work,
                          result=observed, compression=json.loads((home / 'compressed.json').read_text()),
                          refusal=json.loads((home / 'observed.json').read_text()), model_requests=model.main_requests())

        with daemon(root, home, env, barrier=True, fixture='cron_watch_daemon.py') as (proc, desc):
            try:
                asyncio.run(run())
            finally:
                if scenario == 'owner-loss':
                    proc.kill()
                    proc.wait(timeout=10)
                release.set()
                print((home / "first.log").read_text()[-16000:])
                print("TRANSPORT", (home / "transport.jsonl").read_text()[-6000:])
                print("PRIVATE WORK", private_work(home))
                print("MODEL SUMMARY", [(str([m for m in b["messages"] if m["role"] == "user"][-1])[0:250], [m for m in b["messages"][-3:] if m["role"] == "tool"]) for b in model.main_requests()])
                print("PRODUCER", (home / "producer.json").read_text() if (home / "producer.json").exists() else "absent")
        if scenario == 'owner-loss':
            (home / 'observe-restart').touch()
            with daemon(root, home, env, barrier=True, fixture='cron_watch_daemon.py') as (next_proc, next_desc):
                async def observe():
                    jobs_file = home / 'cron/jobs.json'
                    jobs = json.loads(jobs_file.read_text())
                    jobs['jobs'].append(dict(id='observe-old-fire', name='Observe only',
                        prompt='observe old receipt; read the fixture note',
                        schedule={'kind': 'interval', 'minutes': 240},
                        next_run_at=(now() - timedelta(minutes=1)).isoformat(), enabled=True,
                        state='scheduled', deliver='local', repeat={'times': None, 'completed': 0}))
                    jobs_file.write_text(json.dumps(jobs))
                    tick = await asyncio.to_thread(subprocess.run,
                        [sys.executable, '-c', 'from cron.scheduler import tick; tick(verbose=False, sync=True)'],
                        cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
                    assert tick.returncode == 0, tick.stdout + tick.stderr
                    await async_wait_until(lambda: (home / 'observed.json').exists() or any(
                        r['principal_id'] == 'cron-owner' and 'observe-old-fire' in r['request_id'] and r['status'] == 'terminal'
                        for r in rows(home, 'session_admissions')), timeout=35)
                    assert (home / 'observed.json').exists(), dict(tick=tick.stdout + tick.stderr,
                        log=(home / 'first.log').read_text()[-6000:], models=model.main_requests()[-2:])
                asyncio.run(observe())
                result = json.loads((home / 'observed.json').read_text())
                seen = json.loads((home / 'producer.json').read_text())
                # Official unattended recovery resolves fenced unknown as interrupted, never replayed.
                assert result['status'] == 'terminal' and result['outcome'] == 'interrupted', json.dumps(result)
                assert result['completed'] is False and result['model_completed'] is False, result
                assert result['first_tool'] is None
                assert result['admission_id'] == seen['replies'][0]['admission_id']
                assert next_desc['authority_epoch'] > desc['authority_epoch'] and next_proc.pid != proc.pid
                assert len(watch_rows(home)) == 1 and not private_work(home)
                starts = [b for b in model.main_requests() if 'Scheduled task check;' in str(b['messages'][-1])]
                assert len(starts) == 1
                save_evidence(root, tmp_path, scenario=scenario, producer=seen, result=result,
                    pids=[proc.pid, next_proc.pid], epochs=[desc['authority_epoch'], next_desc['authority_epoch']],
                    wake_admissions=watch_rows(home), requests=model.main_requests())


def watch_rows(home):
    return [r for r in rows(home, 'session_admissions') if r['request_id'].startswith('["cron-task-watch"')]


def save_evidence(root, tmp_path, **data):
    evidence = root / '.hermes/watch-entry/evidence'
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / ('native-' + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12] + '.json')).write_text(
        json.dumps(data, indent=2))


async def verify_continuity(home, admission_id, target):
    (home / 'observed.json').unlink()
    (home / 'compress-main').touch()
    await async_wait_until(lambda: (home / 'compressed.json').exists() or (home / 'fixture-error').exists(), timeout=40)
    assert not (home / 'fixture-error').exists(), (home / 'fixture-error').read_text()
    compressed = json.loads((home / 'compressed.json').read_text())
    assert compressed['before'] != compressed['after']
    (home / 'query-compressed').touch()
    await async_wait_until(lambda: (home / 'observed.json').exists(), timeout=25)
    result = json.loads((home / 'observed.json').read_text())
    assert result['completed'] is True and result['admission_id'] == admission_id, result
    (home / 'observed.json').unlink()
    (home / 'reset-target').write_text(target)
    await async_wait_until(lambda: (home / 'reset-ready').exists() or (home / 'fixture-error').exists(), timeout=35)
    assert not (home / 'fixture-error').exists(), (home / 'fixture-error').read_text()
    (home / 'query-reset').touch()
    await async_wait_until(lambda: (home / 'observed.json').exists(), timeout=25)
    result = json.loads((home / 'observed.json').read_text())
    assert result['completed'] is False and 'reset or forked' in result.get('error', ''), result
    assert len(watch_rows(home)) == 1 and len(private_work(home)) == 1
