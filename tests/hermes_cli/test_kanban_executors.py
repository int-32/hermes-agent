"""Kanban executor pool: worktree tasks dispatched to other machines over SSH.

The SSH hop is replaced by running the same bash scripts locally, so remote worktree
creation and cleanup are exercised against real git repos.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_executors as kexec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    kexec._probe_cache.clear()
    kexec._pool_cache = (None, None)
    return home


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def board_repo(tmp_path):
    """The dispatcher host's checkout of the board repo, with a bare ``origin``."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    repo = tmp_path / "brain" / "casepilot"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "k@example.com")
    _git(repo, "config", "user.name", "K")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    return repo


def _local_runner(pool, machine, script, *, timeout=120):
    """``run_script`` without the SSH hop."""
    return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, timeout=timeout,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home()),
                               "GIT_AUTHOR_NAME": "K", "GIT_AUTHOR_EMAIL": "k@example.com",
                               "GIT_COMMITTER_NAME": "K", "GIT_COMMITTER_EMAIL": "k@example.com"})


def _pool(**machines_caps):
    return kexec.parse_pool({
        "key": "~/.ssh/aiyu_executor", "profiles": ["coder"], "boards": ["default"],
        "machines": [{"name": n, "host": f"10.0.0.{i + 1}", "user": "aiyu", "max_in_progress": cap}
                     for i, (n, cap) in enumerate(machines_caps.items())],
    })


# --- config -------------------------------------------------------------------------------

def test_parse_pool_reads_machines_and_scope():
    pool = kexec.parse_pool({
        "key": "/k", "profiles": "coder, coder-opus", "boards": ["casepilot"],
        "machines": [{"name": "brain", "local": True, "max_in_progress": 2},
                     {"name": "aiyu001", "host": "10.105.15.56", "user": "aiyu", "max_in_progress": 2,
                      "port": 2222}],
    })
    assert pool.profiles == {"coder", "coder-opus"}
    assert pool.serves("casepilot", "coder") and not pool.serves("hermes-agent", "coder")
    assert not pool.serves("casepilot", "planner")
    brain, aiyu = pool.machines
    assert brain.local and not aiyu.local
    assert pool.machine_for("ssh://aiyu@10.105.15.56:2222/x") == aiyu
    assert pool.machine_for("ssh://aiyu@10.105.15.56/x") is None  # wrong port


@pytest.mark.parametrize("raw, field", [
    ({"machines": []}, "key"),
    ({"key": "/k", "machines": [{"host": "h"}]}, "host and user"),
    ({"key": "/k", "machines": [{"host": "h", "user": "u", "max_in_progress": 0}]}, ">= 1"),
    ({"key": "/k", "machines": [{"host": "h", "user": "u", "workdir": "rel"}]}, "absolute"),
    ({"key": "/k", "machines": [{"local": True}, {"local": True, "name": "b"}]}, "one local"),
    ({"key": "/k", "machines": [{"host": "h", "user": "u"}, {"host": "h", "user": "v"}]}, "unique"),
])
def test_parse_pool_rejects_bad_entries(raw, field):
    with pytest.raises(ValueError, match=field):
        kexec.parse_pool(raw)


def test_load_pool_reads_the_shared_root_config(kanban_home):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  executor_pool:\n    key: /k\n    profiles: [coder]\n    boards: [default]\n"
        "    machines:\n      - {name: m1, host: h, user: u}\n")
    pool = kexec.load_pool()
    assert pool is not None and pool.machines[0].name == "m1"
    (kanban_home / "config.yaml").write_text("kanban:\n  executor_pool:\n    machines: []\n")
    assert kexec.load_pool() is None  # broken config: no pool, never a crash


def test_uri_round_trip():
    pool = _pool(m1=1)
    uri = kexec.make_uri(pool.machines[0], "/Users/aiyu/workspace/aiyu/casepilot/.worktrees/t_1")
    assert uri == "ssh://aiyu@10.0.0.1/Users/aiyu/workspace/aiyu/casepilot/.worktrees/t_1"
    assert kexec.is_remote(uri) and not kexec.is_remote("/tmp/x") and not kexec.is_remote(None)
    assert kexec.remote_path(uri).endswith("/.worktrees/t_1")
    assert kexec.terminal_overrides(pool, uri) == {
        "TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "10.0.0.1", "TERMINAL_SSH_USER": "aiyu",
        "TERMINAL_SSH_PORT": "22", "TERMINAL_SSH_KEY": str(Path("~/.ssh/aiyu_executor").expanduser()),
        "TERMINAL_SSH_SYNC_FILES": "false", "TERMINAL_CWD": kexec.remote_path(uri)}


# --- choosing a machine -------------------------------------------------------------------

def _running(conn, machine, n):
    for i in range(n):
        tid = kb.create_task(conn, title=f"busy-{machine.name}-{i}", assignee="coder")
        kb.claim_task(conn, tid)
        kbw.set_workspace_path(conn, tid, kexec.make_uri(machine, f"/r/.worktrees/{tid}"))


def test_choose_machine_least_loaded_reachable(kanban_home):
    pool = _pool(m1=2, m2=2, m3=4)
    m1, m2, m3 = pool.machines
    up = {"m1": "/h1", "m2": None, "m3": "/h3"}
    with kbc.connect_closing() as conn:
        _running(conn, m1, 1)
        _running(conn, m3, 1)
        # m2 is emptiest but unreachable; m3 (1/4) beats m1 (1/2).
        assert kexec.choose_machine(pool, conn, None, probe=lambda p, m: up[m.name]) == (m3, "/h3")
        _running(conn, m3, 3)
        assert kexec.choose_machine(pool, conn, None, probe=lambda p, m: up[m.name]) == (m1, "/h1")
        _running(conn, m1, 1)
        assert kexec.choose_machine(pool, conn, None, probe=lambda p, m: up[m.name]) is None


def test_choose_machine_retry_stays_with_its_worktree(kanban_home):
    pool = _pool(m1=1, m2=1)
    m1, m2 = pool.machines
    with kbc.connect_closing() as conn:
        assert kexec.choose_machine(pool, conn, None, prefer=m2, probe=lambda p, m: "/h")[0] == m2
        _running(conn, m2, 1)
        # Full but reachable: wait for it rather than restart elsewhere without its commits.
        assert kexec.choose_machine(pool, conn, None, prefer=m2, probe=lambda p, m: "/h") is None
        # Gone: fall back to another machine.
        assert kexec.choose_machine(pool, conn, None, prefer=m2,
                                    probe=lambda p, m: None if m == m2 else "/h")[0] == m1


def test_local_entry_counts_pool_tasks_and_needs_no_probe(kanban_home):
    pool = kexec.parse_pool({"key": "/k", "profiles": ["coder"], "boards": ["default"], "machines": [
        {"name": "brain", "local": True, "max_in_progress": 1},
        {"name": "m1", "host": "h", "user": "u", "max_in_progress": 1}]})
    brain, m1 = pool.machines
    with kbc.connect_closing() as conn:
        other = kb.create_task(conn, title="planner work", assignee="planner")
        kb.claim_task(conn, other)  # not the pool's work: does not use the brain's slot
        assert kexec.choose_machine(pool, conn, None, probe=lambda p, m: pytest.fail("probed"))[0] == brain
        mine = kb.create_task(conn, title="local", assignee="coder", workspace_kind="worktree")
        kb.claim_task(conn, mine)
        assert kexec.choose_machine(pool, conn, None, probe=lambda p, m: "/h")[0] == m1


def test_remote_home_caches_failures(kanban_home):
    pool = _pool(m1=1)
    calls = []

    def runner(pool, machine, script, *, timeout):
        calls.append(1)
        raise kexec.ExecutorUnreachable("down")

    assert kexec.remote_home(pool, pool.machines[0], now=100.0, runner=runner) is None
    assert kexec.remote_home(pool, pool.machines[0], now=130.0, runner=runner) is None
    assert len(calls) == 1
    kexec.remote_home(pool, pool.machines[0], now=100.0 + kexec.PROBE_TTL_SECONDS, runner=runner)
    assert len(calls) == 2


# --- remote worktrees ---------------------------------------------------------------------

def test_prepare_and_cleanup_remote_worktree(tmp_path, board_repo):
    pool = _pool(m1=1)
    m1 = pool.machines[0]
    home = str(tmp_path / "remote-home")
    uri = kexec.prepare_worktree(pool, m1, home, board_repo, "t_1", "wt/t_1", runner=_local_runner)
    target = Path(kexec.remote_path(uri))
    assert uri.startswith("ssh://aiyu@10.0.0.1/") and target == Path(home) / "workspace/aiyu/casepilot/.worktrees/t_1"
    assert (target / "README.md").exists() and _git(target, "rev-parse", "--abbrev-ref", "HEAD").strip() == "wt/t_1"
    assert ".worktrees/" in (target.parent.parent / ".git/info/exclude").read_text()
    # A retry reuses the checkout, unpushed work included.
    (target / "work.txt").write_text("x")
    _git(target, "add", ".")
    _git(target, "commit", "-qm", "work")
    assert kexec.prepare_worktree(pool, m1, home, board_repo, "t_1", "wt/t_1", runner=_local_runner) == uri
    # Unmerged work is kept; once it is in origin/main the checkout and its wt/ branch go.
    assert kexec.cleanup_worktree("t_1", uri, None, pool=pool, runner=_local_runner) == "unmerged"
    assert target.is_dir()
    _git(target, "push", "-q", "origin", "HEAD:main")
    assert kexec.cleanup_worktree("t_1", uri, None, pool=pool, runner=_local_runner) == "removed"
    assert not target.exists()
    assert "wt/t_1" not in _git(target.parent.parent, "branch")


def test_prepare_starts_from_a_pushed_parent_branch(tmp_path, board_repo):
    _git(board_repo, "checkout", "-qb", "feature")
    (board_repo / "f.txt").write_text("f")
    _git(board_repo, "add", ".")
    _git(board_repo, "commit", "-qm", "feature")
    _git(board_repo, "push", "-q", "origin", "feature")
    pool = _pool(m1=1)
    uri = kexec.prepare_worktree(pool, pool.machines[0], str(tmp_path / "rh"), board_repo, "t_2", "feature",
                                 runner=_local_runner)
    assert (Path(kexec.remote_path(uri)) / "f.txt").exists()


def test_cleanup_never_guesses_outside_a_worktrees_dir():
    pool = _pool(m1=1)
    assert kexec.cleanup_worktree("t", "ssh://aiyu@10.0.0.1/Users/aiyu/repo", None, pool=pool,
                                  runner=lambda *a, **k: pytest.fail("ran")) is None
    assert kexec.cleanup_worktree("t", "ssh://other@10.0.0.9/r/.worktrees/t", None, pool=pool,
                                  runner=lambda *a, **k: pytest.fail("ran")) is None


# --- dispatcher ---------------------------------------------------------------------------

@pytest.fixture
def pooled_board(kanban_home, board_repo, monkeypatch, all_assignees_spawnable):
    kb.write_board_metadata("default", default_workdir=str(board_repo))
    pool = _pool(m1=1)
    monkeypatch.setattr(kexec, "load_pool", lambda: pool)
    monkeypatch.setattr(kexec, "run_script", _local_runner)
    monkeypatch.setattr(kexec, "remote_home", lambda p, m, **_: str(kanban_home.parent / "remote-home"))
    return pool


def test_dispatch_sends_pool_tasks_to_an_executor(pooled_board):
    spawned = []
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="code", assignee="coder", workspace_kind="worktree")
        local = kb.create_task(conn, title="plan", assignee="planner")
        res = kbd.dispatch_once(conn, spawn_fn=lambda t, ws, board=None: spawned.append((t.id, ws)) or 1)
        task = kb.get_task(conn, tid)
    remote_ws = dict(spawned)[tid]
    assert remote_ws == task.workspace_path and kexec.is_remote(remote_ws)
    assert Path(kexec.remote_path(remote_ws)).is_dir()
    assert task.branch_name == f"wt/{tid}"
    assert not kexec.is_remote(dict(spawned)[local])
    assert res.skipped_executor_unavailable == []


def test_dispatch_waits_when_every_executor_is_busy(pooled_board):
    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="a", assignee="coder", workspace_kind="worktree")
        second = kb.create_task(conn, title="b", assignee="coder", workspace_kind="worktree")
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
        waiting = kb.get_task(conn, second)
    assert [s[0] for s in res.spawned] == [first]
    assert res.skipped_executor_unavailable == [second]
    assert waiting.status == "ready" and waiting.consecutive_failures == 0


def test_dispatch_unreachable_executor_spends_no_retry_budget(pooled_board, monkeypatch):
    def down(*a, **k):
        raise kexec.ExecutorUnreachable("m1: ssh failed")

    monkeypatch.setattr(kexec, "run_script", down)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="a", assignee="coder", workspace_kind="worktree")
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: pytest.fail("spawned"))
        task = kb.get_task(conn, tid)
    assert not res.spawned and task.status == "ready" and task.consecutive_failures == 0
    assert kexec._probe_cache["m1"][1] is None  # skipped until the probe expires


def test_remote_path_does_not_break_a_later_local_attempt(kanban_home, board_repo, all_assignees_spawnable):
    kb.write_board_metadata("default", default_workdir=str(board_repo))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="a", assignee="coder", workspace_kind="worktree")
        kbw.set_workspace_path(conn, tid, "ssh://aiyu@10.0.0.1/x/.worktrees/" + tid)
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)  # no pool configured any more
    assert res.spawned[0][2] == str((board_repo / ".worktrees" / tid).resolve())


# --- worker side --------------------------------------------------------------------------

def test_ssh_commands_carry_the_kanban_task_identity(monkeypatch):
    from tools.environments import ssh
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/Users/aiyu/w/.worktrees/t_1")
    monkeypatch.delenv("HERMES_KANBAN_BRANCH", raising=False)
    assert ssh._prepend_kanban_env("pwd") == (
        "export HERMES_KANBAN_TASK=t_1\nexport HERMES_KANBAN_WORKSPACE=/Users/aiyu/w/.worktrees/t_1\npwd")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE")
    assert ssh._prepend_kanban_env("pwd") == "pwd"


def test_ssh_sync_files_flag_reaches_the_backend(monkeypatch):
    from tools import terminal_tool_backends as backends
    seen = {}
    monkeypatch.setattr(backends, "_SSHEnvironment", lambda **kw: seen.update(kw))
    config = {"ssh_host": "h", "ssh_user": "u", "ssh_sync_files": False}
    backends._build_ssh_env(cwd="/w", timeout=10, ssh_config=backends._ssh_config_from_config(config))
    assert seen["sync_files"] is False


def test_worker_context_explains_remote_workspace_and_retry(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="a", assignee="coder", workspace_kind="worktree")
        kbw.set_workspace_path(conn, tid, "ssh://aiyu@10.0.0.1/Users/aiyu/w/.worktrees/" + tid)
        kb.claim_task(conn, tid)
        kbd._record_task_failure(conn, tid, "boom", outcome="crashed", release_claim=True, end_run=True)
        kb.claim_task(conn, tid)
        text = kb.build_worker_context(conn, tid)
    assert f"/Users/aiyu/w/.worktrees/{tid} on executor machine aiyu@10.0.0.1" in text
    assert "Never repeat an external action" in text
