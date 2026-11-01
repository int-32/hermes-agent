"""No-args wrapper through real cron execution, redaction, gate and prompt assembly."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
WRAPPER = ROOT / "scripts/task_owner_gate.py"
COLLECTOR = ROOT / "scripts/task_owner_snapshot.py"


def write_hook(directory, binding, control, trace):
    """Test-only clock/import instrumentation; never adds a Hermes import root."""
    directory.mkdir()
    (directory / "sitecustomize.py").write_text('''import atexit, hashlib, json, sys, time
from pathlib import Path
control = json.loads(Path(CONTROL).read_text())
time.time = lambda: control['clock']
sys.path[:] = [p for p in sys.path if p != ROOT and not p.startswith('__editable__')]
sys.meta_path[:] = [f for f in sys.meta_path if not getattr(f, '__module__', '').startswith('__editable__')]
state = {'collector_main': 0, 'atomic_calls': 0, 'start_path': list(sys.path)}
def profile(frame, event, arg):
    if event != 'call': return
    file, name = frame.f_code.co_filename, frame.f_code.co_name
    if file == COLLECTOR and name == 'main': state['collector_main'] += 1
    if file == WRAPPER and name == 'main': state['wrapper_path'] = list(sys.path)
    if name == 'atomic_write_text' and file.endswith('/utils.py'):
        state['atomic_calls'] += 1
        state['writer_file'] = file
def audit(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'): raise PermissionError('offline fixture')
sys.addaudithook(audit)
sys.setprofile(profile)
if control.get('missing_dependency'):
    class Missing:
        def find_spec(self, name, *args):
            if name == 'hermes_yaml': raise ImportError('fixture missing dependency')
    sys.meta_path.insert(0, Missing())
if control.get('alias_directory'):
    import tempfile
    tempfile.mkdtemp = lambda **kw: control['alias_directory']
def finish():
    sys.setprofile(None)
    state['executable'] = sys.executable
    state['sources'] = {}
    for name in ('utils', 'hermes_yaml', 'ruamel.yaml'):
        module = sys.modules.get(name)
        if module is not None:
            path = Path(module.__file__)
            state['sources'][name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    Path(TRACE).write_text(json.dumps(state))
atexit.register(finish)
'''.replace('CONTROL', repr(str(control))).replace('TRACE', repr(str(trace)))
        .replace('ROOT', repr(str(ROOT))).replace('COLLECTOR', repr(binding['collector']))
        .replace('WRAPPER', repr(str(Path(binding['collector']).with_name('task_owner_gate.py')))))


class Bundle(SimpleNamespace):
    def fire(self, clock=1000, **faults):
        self.control.write_text(json.dumps(dict(clock=clock, **faults)))
        self.trace.unlink(missing_ok=True)
        self.argvs.clear()
        before = (self.checkpoint.read_bytes(), self.snapshot.read_bytes())
        result = self.script._run_job_script(str(self.wrapper))
        provenance = json.loads(self.trace.read_text()) if self.trace.exists() else {}
        assert (self.checkpoint.read_bytes(), self.snapshot.read_bytes()) == before
        self.node.user_properties.append(('wrapper_execution', json.dumps({
            'branch': self.branch, 'argv': self.argvs, 'provenance': provenance,
            'consumer': str(self.script.__file__),
            'consumer_sha256': hashlib.sha256(Path(self.script.__file__).read_bytes()).hexdigest(),
            'wrapper_sha256': hashlib.sha256(self.wrapper.read_bytes()).hexdigest() if self.wrapper.exists() else None,
            'collector_sha256': hashlib.sha256(self.collector.read_bytes()).hexdigest(),
            'checkpoint_and_baseline_unchanged': True, 'success': result[0]})))
        return (*result, provenance)

    def prompt(self, result):
        spec = importlib.util.spec_from_file_location('gate_template', WRAPPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        job = dict(id='abc123', script=str(self.wrapper), prompt=module.processing_prompt(self.checkpoint), context_from=['self'])
        return self.prompt_module._build_job_prompt(job, prerun_script=result)

    def value(self, output):
        return json.loads(output.splitlines()[-1])


def seed(home, output):
    with sqlite3.connect(home / 'kanban.db') as conn:
        conn.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT);INSERT INTO tasks VALUES('task','running')")
    args = [sys.executable, str(COLLECTOR), '--home', str(home), '--now', '1000']
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False,
                          env=dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home)))
    assert proc.returncode == 0, proc.stderr
    snapshot = output / 'evidence-consumed' / 'snapshot.json'
    snapshot.parent.mkdir()
    snapshot.write_text(proc.stdout)
    checkpoint = output / 'checkpoint.json'
    record = {key: {} for key in ('actual_actions', 'actual_coverage', 'evidence_paths', 'failed_or_unverified',
        'new_user_action_required', 'notification', 'owner_observations', 'previous_entry_evidence',
        'scope_preserved', 'user_action_required', 'wake_verification')}
    record.update(schema='task-owner-inspection-checkpoint/v2', observed_at='1970-01-01T00:16:40+00:00',
                  job_id='abc123', evidence_root=str(snapshot.parent),
                  consumed_fingerprints={'snapshot_path': str(snapshot), 'existing_owner_field': 'retain'})
    checkpoint.write_text(json.dumps(record))
    return checkpoint, snapshot


@pytest.fixture(params=['bare', 'managed'])
def bundle(tmp_path, monkeypatch, request):
    from cron import jobs, scheduler, scheduler_script, scheduler_prompt
    import hermes_cli._launchers as launchers
    from tools.environments import local

    home = tmp_path / 'home'
    scripts = home / 'scripts/task-owner'
    scripts.mkdir(parents=True)
    output = home / 'cron/output/abc123'
    output.mkdir(parents=True)
    checkpoint, snapshot = seed(home, output)
    # Bind the complete approved source root: atomic_write_text lazily imports hermes_constants too.
    source = ROOT
    wrapper = scripts / 'task_owner_gate.py'
    if WRAPPER.exists():
        shutil.copyfile(WRAPPER, wrapper)
    collector = scripts / 'task_owner_snapshot.py'
    shutil.copyfile(COLLECTOR, collector)
    binding = dict(home=str(home), collector=str(collector), checkpoint=str(checkpoint),
                   evidence_directory=str(output), source_root=str(source))
    wrapper.with_suffix('.json').write_text(json.dumps(binding))
    control, trace = tmp_path / 'control.json', tmp_path / 'trace.json'
    hooks = tmp_path / 'hooks'
    write_hook(hooks, binding, control, trace)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: home)
    monkeypatch.setattr(scheduler, '_SCRIPT_TIMEOUT', 15)
    for key, value in dict(HERMES_DIR=home, CRON_DIR=home/'cron', JOBS_FILE=home/'cron/jobs.json',
                           OUTPUT_DIR=home/'cron/output').items():
        monkeypatch.setattr(jobs, key, value)
    managed = request.param == 'managed'
    monkeypatch.setattr(launchers, 'resolve_store_python', lambda repo: Path(sys.executable) if managed else None)
    selections = []
    def selected(repo):
        selections.append(str(repo))
        return Path(sys.executable)
    monkeypatch.setitem(sys.modules, 'pm.environments', SimpleNamespace(project_python=selected))
    env = {key: value for key, value in os.environ.items() if key in ('PATH', 'LANG', 'TZ', 'HOME', 'HERMES_HOME')}
    env['PYTHONPATH'] = str(hooks)  # only test clock/audit hook, no Hermes code/dependencies
    monkeypatch.setattr(local, 'build_subprocess_env', lambda **kw: env.copy())
    argvs, real_popen = [], scheduler_script.subprocess.Popen
    def popen(argv, *args, **kwargs):
        argvs.append(list(argv))
        return real_popen(argv, *args, **kwargs)
    monkeypatch.setattr(scheduler_script.subprocess, 'Popen', popen)
    original = (checkpoint.read_bytes(), snapshot.read_bytes())
    b = Bundle(home=home, output=output, source=source, wrapper=wrapper, collector=collector,
        binding=binding, checkpoint=checkpoint, snapshot=snapshot, control=control, trace=trace,
        script=scheduler_script, prompt_module=scheduler_prompt, branch=request.param,
        argvs=argvs, selections=selections, node=request.node)
    yield b
    assert (checkpoint.read_bytes(), snapshot.read_bytes()) == original


def test_no_args_wrapper_real_branches_and_same_clock_false(bundle):
    ok, stdout, provenance = bundle.fire()
    assert ok, stdout
    value = bundle.value(stdout)
    assert value['wakeAgent'] is False and bundle.prompt_module._parse_wake_gate(stdout) is False
    assert stdout.isascii() and len(stdout.splitlines()) == 1
    assert provenance['collector_main'] == provenance['atomic_calls'] == 1
    assert provenance['sources']['utils']['path'] == str(bundle.source / 'utils.py')
    assert provenance['sources']['utils']['sha256'] == hashlib.sha256((bundle.source/'utils.py').read_bytes()).hexdigest()
    assert provenance['sources']['hermes_yaml']['path'] == str(bundle.source / 'hermes_yaml.py')
    assert 'ruamel.yaml' in provenance['sources']
    assert len(bundle.argvs) == 1
    if bundle.branch == 'bare':
        assert bundle.argvs[0] == [sys.executable, str(bundle.wrapper)]
        assert str(ROOT) not in provenance['wrapper_path'] and bundle.selections == []
    else:
        assert bundle.argvs[0][0:2] == [sys.executable, '-c']
        assert bundle.argvs[0][-2:] == [str(ROOT), str(bundle.wrapper)]
        assert str(ROOT) in provenance['wrapper_path'] and bundle.selections == [str(ROOT)]
    artifact = value['snapshot_artifact']
    raw = Path(artifact['path']).read_bytes()
    assert len(raw) == artifact['bytes'] and hashlib.sha256(raw).hexdigest() == artifact['sha256']
    assert Path(artifact['path']).parent.parent == bundle.output
    again, second, counts = bundle.fire()
    assert again and bundle.value(second)['wakeAgent'] is False
    assert counts['collector_main'] == 1
    assert bundle.value(second)['snapshot_artifact']['path'] != artifact['path']
    assert Path(artifact['path']).read_bytes() == raw


@pytest.mark.parametrize('case', ['changed', 'due', 'invalid_db', 'unknown_coverage', 'omitted', 'backlog'])
def test_wake_error_and_full_candidates_never_promote_checkpoint(bundle, case):
    def rows(count):
        with sqlite3.connect(bundle.home/'kanban.db') as conn:
            conn.executemany("INSERT INTO tasks VALUES(?, 'done')", [(str(i),) for i in range(count)])
    def backlog():
        worker = bundle.home/'workers/w1'
        worker.mkdir(parents=True)
        (worker/'meta.json').write_text('{"id":"w1"}')
        (worker/'events.jsonl').write_text((json.dumps({'kind': 'tool', 'data': 'a'*1000})+'\n')*102)
    mutate = {
        'invalid_db': lambda: (bundle.home/'kanban.db').write_text('not sqlite'),
        'unknown_coverage': lambda: (bundle.home/'kanban.db').rename(bundle.home/'unavailable.db'),
        'changed': lambda: rows(1), 'omitted': lambda: rows(25), 'backlog': backlog, 'due': lambda: None,
    }
    mutate[case]()
    ok, stdout, provenance = bundle.fire(clock=2800 if case == 'due' else 1000)
    value = bundle.value(stdout)
    assert ok == (case not in ('invalid_db', 'unknown_coverage'))
    assert value['wakeAgent'] is True and bundle.prompt_module._parse_wake_gate(stdout) is True
    assert provenance['collector_main'] == 1
    assert value['snapshot_artifact'] is not None and Path(value['snapshot_artifact']['path']).is_file()
    if case == 'due':
        assert 'review_due' in value['reasons'] and not any(value['delta'].values())
    if case in ('invalid_db', 'unknown_coverage', 'omitted', 'backlog'):
        assert value['requires_reconciliation'] is True
    if case == 'omitted':
        assert sum(value['omitted'].values()) > 0
    # Actual prompt assembler receives the cached result; it must not execute a second script.
    before = len(bundle.argvs)
    prompt = bundle.prompt((ok, stdout))
    assert stdout in prompt, (stdout, prompt)
    assert str(bundle.checkpoint) in prompt
    assert ('Script Output' if ok else 'Script Error') in prompt
    assert len(bundle.argvs) == before


def test_redaction_unicode_and_legacy_self_context_do_not_rerun(bundle):
    worker = bundle.home/'workers/w1'
    worker.mkdir(parents=True)
    (worker/'meta.json').write_text('{"id":"w1"}')
    needle = 'sk-' + 'a'*48
    (worker/'events.jsonl').write_text(json.dumps({'kind': 'turn_done', 'text': needle+'\u2028\u0085'})+'\n')
    (bundle.output/'old.md').write_text('Legacy workflow used an existing collector; do not treat this history as retirement proof.\n')
    ok, stdout, provenance = bundle.fire()
    assert ok and needle not in stdout and stdout.isascii() and len(stdout.splitlines()) == 1
    artifact = bundle.value(stdout)['snapshot_artifact']
    assert needle in Path(artifact['path']).read_text()
    prompt = bundle.prompt((ok, stdout))
    assert 'Legacy workflow' in prompt and '只消费本轮' in prompt and '不得再次运行' in prompt
    assert provenance['collector_main'] == 1 and len(bundle.argvs) == 1


def test_missing_dependency_is_explicit_error_not_false_or_ack(bundle):
    ok, stdout, provenance = bundle.fire(missing_dependency=True)
    assert not ok and 'Script exited with code 2' in stdout
    assert bundle.prompt_module._parse_wake_gate(stdout) is True
    assert provenance['collector_main'] == 0
    assert set(bundle.output.glob('evidence-*/snapshot.json')) == {bundle.snapshot}


@pytest.mark.parametrize('target', ['checkpoint', 'baseline', 'database'])
def test_collector_alias_guard_survives_wrapper(bundle, target):
    directory = bundle.output / 'forced-candidate'
    directory.mkdir()
    victim = {'checkpoint': bundle.checkpoint, 'baseline': bundle.snapshot, 'database': bundle.home/'kanban.db'}[target]
    os.link(victim, directory/'snapshot.json')
    before = victim.read_bytes()
    ok, stdout, provenance = bundle.fire(alias_directory=str(directory))
    assert not ok and bundle.value(stdout)['snapshot_artifact'] is None
    assert 'snapshot_input_alias' in stdout and provenance['collector_main'] == 1
    assert victim.read_bytes() == before


def test_legacy_v1_deadline_fallback_through_v2_checkpoint(bundle):
    original = bundle.snapshot.read_bytes()
    old = json.loads(original)
    old.pop('next_review_at')
    bundle.snapshot.write_text(json.dumps(old))
    # Restore the test-owned fixture after verifying compatibility.
    try:
        ok, stdout, _ = bundle.fire()
        assert ok and bundle.value(stdout)['wakeAgent'] is False
        assert bundle.value(stdout)['next_review_at'] == 2800
    finally:
        bundle.snapshot.write_bytes(original)


def test_single_job_forward_and_rollback_payloads_use_real_update(bundle):
    from cron.jobs import create_job, get_job
    from tools.cronjob_tools import cronjob
    spec = importlib.util.spec_from_file_location('gate_template', WRAPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (bundle.home/'scripts/legacy.py').write_text('print("legacy fixture")\n')
    old = create_job(prompt='Original processing instruction', schedule='every 30m',
                     monitor_script='legacy.py', no_agent=False, deliver='local')
    relative = str(bundle.wrapper.relative_to(bundle.home / 'scripts'))
    forward, rollback = module.job_payloads(old, relative, bundle.checkpoint)
    preserved = {key: old.get(key) for key in ('schedule', 'model', 'provider', 'deliver', 'interpreter', 'workdir')}
    assert forward['monitor_script'] == forward['monitor_url'] == ''
    assert forward['no_agent'] is False
    applied = json.loads(cronjob(**forward))
    assert applied['success'] is True, applied
    updated = get_job(old['id'])
    assert not updated['monitor_script'] and not updated['monitor_url'] and updated['script'] == relative
    assert {key: updated.get(key) for key in preserved} == preserved
    undone = json.loads(cronjob(**rollback))
    assert undone['success'] is True, undone
    restored = get_job(old['id'])
    for key in ('script', 'monitor_script', 'monitor_url', 'no_agent', 'prompt'):
        assert restored.get(key) == old.get(key)
    assert {key: restored.get(key) for key in preserved} == preserved


@pytest.mark.parametrize('fault', ['relative_binding', 'missing_evidence_directory'])
def test_preparation_failure_is_nonzero_without_collector_or_checkpoint_write(bundle, fault):
    if fault == 'relative_binding':
        binding = dict(bundle.binding, checkpoint='relative-checkpoint.json')
        bundle.wrapper.with_suffix('.json').write_text(json.dumps(binding))
    else:
        binding = dict(bundle.binding, evidence_directory=str(bundle.home/'absent-evidence'))
        bundle.wrapper.with_suffix('.json').write_text(json.dumps(binding))
    ok, stdout, provenance = bundle.fire()
    assert not ok and 'Script exited with code 2' in stdout
    assert bundle.prompt_module._parse_wake_gate(stdout) is True
    assert provenance['collector_main'] == 0
    assert set(bundle.output.glob('evidence-*/snapshot.json')) == {bundle.snapshot}
