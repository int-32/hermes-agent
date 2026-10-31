"""Canonical cron -> official plugin child -> durable gateway injection contract."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

pytestmark = pytest.mark.platforms("posix")

from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home
from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket
from tests.waiting import async_wait_until


PLUGIN = '''
import json, os
from pathlib import Path

def register(ctx):
    def inspect(**kwargs):
        fire = ctx.current_cron_execution()
        if fire is None:
            return
        home = Path(os.environ["HERMES_HOME"])
        target = json.loads((home / "target.json").read_text())
        # Legacy bool acknowledges scheduling, deliberately without stable retry identity.
        replies = [ctx.inject_message("WATCH_DELTA", session_key=target["route"]) for _ in range(2)]
        (home / "producer.json").write_text(json.dumps({
            "execution_id": fire.execution_id, "profile": fire.profile,
            "job_id": fire.job_id, "source": fire.source, "replies": replies}))
    ctx.register_hook("pre_tool_call", inspect)
'''


def test_legacy_boolean_calls_remain_distinct_scheduling_acknowledgements(tmp_path):
    from hermes_time import now

    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / "home", tmp_path / "user"
    user.mkdir()
    plugin = home / "plugins" / "watch-probe"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: watch-probe\nversion: 0.0.1\n")
    (plugin / "__init__.py").write_text(PLUGIN)
    note = home / "note.txt"
    with FakeLLMServer([ToolCall("read_file", {"path": str(note)}), Text("cron done")]) as model:
        write_hermes_home(home, model.base_url, extra_config="""
gateway:
  multiplex_profiles: false
plugins:
  isolation: host
  enabled: [watch-probe]
  entries:
    watch-probe:
      allow_gateway_injection: true
""")
        note.write_text("fixture-only delta\n")
        runtime = Path("/tmp") / ("hwe-" + hashlib.sha256(str(root).encode()).hexdigest()[:12])  # no-tmp: ok — official AF_UNIX socket length limit
        runtime.mkdir(mode=0o700, exist_ok=True)
        env = child_env()
        env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
                   HERMES_RUNTIME_DIR=str(tmp_path / "empty-pm"), PYTHONPATH=str(root),
                   PYTHONUNBUFFERED="1",
                   XDG_RUNTIME_DIR=str(runtime))

        def admissions():
            with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                return [dict(row) for row in db.execute(
                    "SELECT * FROM session_admissions WHERE principal_id LIKE 'automation:%' ORDER BY seq")]

        async def run(desc):
            async with websocket(home, desc, proxy=None) as ws:
                created = await rpc(ws, "session.create", request_id="fixture-target", source="gui",
                                    cwd=str(home), toolsets=[])
                assert "result" in created, created
                sid = created["result"]["session_id"]
                # Use the real SessionStore producer's persisted route, never synthesize a SID/key.
                with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as db:
                    routes = db.execute("SELECT session_key FROM gateway_routing WHERE json_extract(entry_json, '$.session_id')=?", (sid,)).fetchall()
                assert len(routes) == 1, routes
                (home / "target.json").write_text(json.dumps({"route": routes[0][0]}))
                (home / "cron").mkdir(exist_ok=True)
                (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
                    "id": "watch-fixture", "name": "watch fixture", "prompt": "read the fixture note",
                    "schedule": {"kind": "interval", "minutes": 240},
                    "next_run_at": (now() - timedelta(minutes=1)).isoformat(),
                    "enabled": True, "state": "scheduled", "deliver": "local",
                    "repeat": {"times": None, "completed": 0}}]}))
                tick = await asyncio.to_thread(subprocess.run,
                    [sys.executable, "-c", "from cron.scheduler import tick; tick(verbose=False, sync=True)"],
                    cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
                assert tick.returncode == 0, tick.stdout + tick.stderr
                seen = json.loads((home / "producer.json").read_text())
                assert seen["job_id"] == "watch-fixture" and seen["source"] == "builtin", seen
                assert seen["replies"] == [True, True], seen
                await async_wait_until(lambda: len(admissions()) == 2, timeout=15)
                rows = admissions()
                (home / "watch-evidence.json").write_text(json.dumps({"producer": seen, "admissions": rows}, indent=2))
                assert len(rows) == 2, {"producer": seen, "admissions": rows}

        with daemon(root, home, env, barrier=False) as (_, desc):
            asyncio.run(run(desc))
