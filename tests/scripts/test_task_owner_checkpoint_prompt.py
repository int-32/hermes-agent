"""One verifier command in the real cached cron prompt, plus single-job delta."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from scripts.task_owner_gate import processing_prompt, job_payloads

pytest_plugins = ['tests.scripts.test_task_owner_gate']
ROOT = Path(__file__).resolve().parents[2]


def test_cached_real_consumer_prompt_executes_one_verify_and_no_second_collector(bundle, tmp_path):
    worker = bundle.home/'workers/w1';worker.mkdir(parents=True)
    (worker/'meta.json').write_text('{"id":"w1"}')
    (worker/'events.jsonl').write_text('{"kind":"turn_done","text":"result\\u2028\\u0085"}\n')
    ok, stdout, counts = bundle.fire()
    assert ok and bundle.prompt_module._parse_wake_gate(stdout) is True
    artifact = json.loads(stdout)['snapshot_artifact']
    directory = Path(artifact['path']).parent
    (directory/'projection.json').write_text(stdout)
    instruction = processing_prompt(bundle.checkpoint, python=sys.executable, source_root=ROOT)
    job = dict(id='abc123', prompt=instruction, script=str(bundle.wrapper), context_from=['self'])
    prompt = bundle.prompt_module._build_job_prompt(job, prerun_script=(ok,stdout))
    commands = re.findall(r'```sh\n(.*?)\n```', instruction, re.DOTALL)
    assert len(commands) == 3 and instruction.count('task_owner_verify.py') == 1
    assert len(bundle.argvs) == 1
    before = bundle.checkpoint.read_bytes()
    proc = subprocess.run(['sh','-c',commands[0]],cwd=ROOT,capture_output=True,text=True,check=False,
                          env=dict(os.environ,D=str(directory)))
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report['ready_for_processing'] and report['checkpoint_written'] is False
    assert report['consumption'] == report['native_ack'] == 'UNVERIFIED'
    assert report['snapshot_artifact'] == artifact
    assert artifact['sha256'] in prompt and 'Script Output' in prompt
    assert counts['collector_main'] == 1 and len(bundle.argvs) == 2
    assert bundle.argvs[1][:2] == ['sh', '-c'] and 'task_owner_verify.py' in bundle.argvs[1][2]
    assert bundle.checkpoint.read_bytes() == before
    assert 'processed=true' not in instruction
    assert 'exit2' in instruction and 'exit3' in instruction and 'context_from=self' in instruction
    # Data-only field delta and exact rollback, through actual existing update consumer.
    from cron import jobs
    original = dict(id='abc123',no_agent=False,prompt='accepted old prompt',script='',monitor_script='old-monitor',
                    monitor_url='',schedule={'kind':'interval','seconds':1800},provider='fake-provider',deliver='origin')
    forward, rollback = job_payloads(original,bundle.wrapper,bundle.checkpoint,python=sys.executable,source_root=ROOT)
    assert forward['prompt'] == instruction and forward['monitor_script'] == forward['monitor_url'] == ''
    assert not {'schedule','provider','deliver'} & forward.keys()
    job_id = jobs.create_job(prompt=original['prompt'],schedule='every 30m',no_agent=False)['id']
    forward['job_id'] = rollback['job_id'] = job_id
    assert jobs.update_job(job_id,{k:v for k,v in forward.items() if k not in ('action','job_id')})
    assert jobs.update_job(job_id,{k:v for k,v in rollback.items() if k not in ('action','job_id')})
    restored = jobs.get_job(job_id)
    assert restored['prompt'] == original['prompt'] and restored['no_agent'] is False
