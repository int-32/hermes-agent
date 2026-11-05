"""Kanban executor pool: run worktree tasks' commands on other machines over SSH.

The dispatcher host stays the only Kanban writer and runs every worker's agent loop; an
executor machine only receives the worker's terminal and file-tool commands (Hermes' SSH
terminal backend) and holds the task's git worktree. Configured under ``kanban.executor_pool`` in
the Kanban root's ``config.yaml`` (``~/.hermes/config.yaml``, shared by every profile like the boards
themselves: the dispatcher and each assignee's worker session must read the same pool)::

    executor_pool:
      key: ~/.ssh/aiyu_executor          # private key the dispatcher host logs in with
      profiles: [coder, coder-opus]      # assignees whose worktree tasks go to executors
      boards: [casepilot, feige-new]     # boards whose repos the executors serve
      machines:
        - name: brain                    # this host itself, in the same pool
          local: true
          max_in_progress: 2
        - name: aiyu001
          host: 10.105.15.56
          user: aiyu
          max_in_progress: 2             # concurrent tasks on this machine
          # port: 22                     # optional
          # workdir: /Users/aiyu/workspace/aiyu   # optional; default ~/workspace/aiyu

A task goes through the pool when its board and assignee are listed and it is a ``worktree``
task on the board's ``default_workdir`` repo. The machine is the reachable one with a free slot
and the lowest load (running / max_in_progress); a ``local: true`` entry is the dispatcher host
itself, competing for tasks on the same terms and running them in its own local worktree. With none available the task waits in
``ready``; nothing counts as a failure. The task's ``workspace_path`` records where the worktree
lives as ``ssh://user@host[:port]/abs/path`` (the machine is identified by user, host and port).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

URI_SCHEME = "ssh://"
DEFAULT_WORKDIR = "workspace/aiyu"  # relative to the remote home
PROBE_TTL_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 5


class ExecutorError(RuntimeError):
    """An executor could not do what was asked (unreachable, git failure)."""


class ExecutorUnreachable(ExecutorError):
    """The SSH connection itself failed: a host condition, not the task's fault."""


@dataclass(frozen=True)
class Machine:
    name: str
    host: str
    user: str
    port: int = 22
    max_in_progress: int = 1
    workdir: Optional[str] = None  # absolute; None = <remote home>/workspace/aiyu
    local: bool = False  # the dispatcher host itself: no SSH, the ordinary local worktree

    def matches(self, user: Optional[str], host: Optional[str], port: Optional[int]) -> bool:
        return not self.local and self.user == user and self.host == host and self.port == (port or 22)


@dataclass(frozen=True)
class Pool:
    key: str
    profiles: frozenset
    boards: frozenset
    machines: tuple

    def serves(self, board: Optional[str], assignee: Optional[str]) -> bool:
        return bool(self.machines) and (board or "default") in self.boards and assignee in self.profiles

    def machine_for(self, uri: str) -> Optional[Machine]:
        parts = urlsplit(uri)
        return next((m for m in self.machines if m.matches(parts.username, parts.hostname, parts.port)), None)


# --- configuration -------------------------------------------------------------------------

def parse_pool(raw: Any) -> Optional[Pool]:
    """``kanban.executor_pool`` -> :class:`Pool`, or None when unset. Invalid entries raise
    ``ValueError`` naming the field, so a typo never silently sends work to the wrong place."""
    if not raw:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("kanban.executor_pool must be a mapping")
    key = str(raw.get("key") or "").strip()
    if not key:
        raise ValueError("kanban.executor_pool.key is required (the dispatcher host's private key)")
    machines = []
    for i, entry in enumerate(raw.get("machines") or []):
        if not isinstance(entry, Mapping):
            raise ValueError(f"kanban.executor_pool.machines[{i}] must be a mapping")
        local = entry.get("local") is True
        host, user = str(entry.get("host") or "").strip(), str(entry.get("user") or "").strip()
        if not local and (not host or not user):
            raise ValueError(f"kanban.executor_pool.machines[{i}] needs host and user (or local: true)")
        try:
            port = int(entry.get("port", 22))
            cap = int(entry.get("max_in_progress", 1))
        except (TypeError, ValueError):
            raise ValueError(f"kanban.executor_pool.machines[{i}]: port and max_in_progress must be integers")
        if cap < 1:
            raise ValueError(f"kanban.executor_pool.machines[{i}].max_in_progress must be >= 1")
        workdir = str(entry.get("workdir") or "").strip() or None
        if workdir and not workdir.startswith("/"):
            raise ValueError(f"kanban.executor_pool.machines[{i}].workdir must be absolute")
        machines.append(Machine(name=str(entry.get("name") or host or "local"), host=host, user=user,
                                port=port, max_in_progress=cap, workdir=workdir, local=local))
    names = [m.name for m in machines]
    if len(set(names)) != len(names):
        raise ValueError("kanban.executor_pool.machines: names must be unique")
    if sum(m.local for m in machines) > 1:
        raise ValueError("kanban.executor_pool.machines: at most one local: true entry")
    return Pool(key=os.path.expanduser(key), profiles=frozenset(_names(raw.get("profiles"))),
                boards=frozenset(_names(raw.get("boards"))), machines=tuple(machines))


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in (value or []) if str(v).strip()]


_pool_cache: tuple[Optional[tuple[int, int]], Optional[Pool]] = (None, None)


def load_pool() -> Optional[Pool]:
    """The configured pool, or None. Re-read when the root ``config.yaml`` changes. A broken
    config is logged and treated as no pool: tasks then run on the dispatcher host as before
    rather than stalling."""
    global _pool_cache
    from hermes_cli import kanban_db as kb

    path = kb.kanban_home() / "config.yaml"
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    if _pool_cache[0] == stamp:
        return _pool_cache[1]
    import hermes_yaml as yaml

    pool = None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        kanban = raw.get("kanban") if isinstance(raw, Mapping) else None
        pool = parse_pool((kanban or {}).get("executor_pool") if isinstance(kanban, Mapping) else None)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        _log.warning("kanban executor pool disabled: %s", exc)
    _pool_cache = (stamp, pool)
    return pool


# --- workspace URIs ------------------------------------------------------------------------

def is_remote(path: Optional[str]) -> bool:
    return bool(path) and str(path).startswith(URI_SCHEME)


def make_uri(machine: Machine, path: str) -> str:
    port = f":{machine.port}" if machine.port != 22 else ""
    return f"{URI_SCHEME}{machine.user}@{machine.host}{port}{path}"


def remote_path(uri: str) -> str:
    return urlsplit(uri).path


# --- SSH -----------------------------------------------------------------------------------

def _ssh_argv(pool: Pool, machine: Machine) -> list[str]:
    argv = ["ssh", "-i", pool.key, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if machine.port != 22:
        argv += ["-p", str(machine.port)]
    return argv + [f"{machine.user}@{machine.host}", "/bin/bash", "-s"]


def run_script(pool: Pool, machine: Machine, script: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a bash script on ``machine`` (fed on stdin, so no quoting through ssh's argv).
    ssh's own exit 255 means the connection failed -> :class:`ExecutorUnreachable`."""
    try:
        result = subprocess.run(_ssh_argv(pool, machine), input=script, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ExecutorUnreachable(f"{machine.name}: ssh timed out after {timeout}s") from exc
    except OSError as exc:
        raise ExecutorUnreachable(f"{machine.name}: cannot run ssh: {exc}") from exc
    if result.returncode == 255:
        raise ExecutorUnreachable(f"{machine.name}: ssh failed: {(result.stderr or '').strip()}")
    return result


_probe_cache: dict[str, tuple[float, Optional[str]]] = {}
_probe_lock = threading.Lock()


def remote_home(pool: Pool, machine: Machine, *, now: Optional[float] = None,
                runner: Callable[..., subprocess.CompletedProcess] = None) -> Optional[str]:
    """The machine's home directory, or None when it is unreachable. Cached for
    ``PROBE_TTL_SECONDS`` either way, so a down machine costs one connect timeout a minute."""
    now = time.monotonic() if now is None else now
    with _probe_lock:
        hit = _probe_cache.get(machine.name)
        if hit and now - hit[0] < PROBE_TTL_SECONDS:
            return hit[1]
    home = None
    try:
        result = (runner or run_script)(pool, machine, 'printf "%s" "$HOME"', timeout=CONNECT_TIMEOUT_SECONDS + 10)
        if result.returncode == 0 and result.stdout.strip().startswith("/"):
            home = result.stdout.strip()
    except ExecutorError as exc:
        _log.info("kanban executor %s unreachable: %s", machine.name, exc)
    with _probe_lock:
        _probe_cache[machine.name] = (now, home)
    return home


def mark_unreachable(machine: Machine) -> None:
    """Record a failed connection so the next ticks skip ``machine`` until the probe expires."""
    with _probe_lock:
        _probe_cache[machine.name] = (time.monotonic(), None)


def machine_workdir(machine: Machine, home: str) -> str:
    return machine.workdir or f"{home.rstrip('/')}/{DEFAULT_WORKDIR}"


# --- choosing a machine --------------------------------------------------------------------

def running_by_machine(pool: Pool, conn, board: Optional[str]) -> dict[str, int]:
    """Running tasks per machine across every board (a machine's slots are shared by boards).
    Remote ones are read from ``workspace_path`` URIs; the local entry counts the pool's own kind of
    task (a pool profile's local worktree on a pool board), so the host's other work does not use
    its slots. Fails open per board like the host-cap count."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    counts: dict[str, int] = {m.name: 0 for m in pool.machines}
    local = next((m for m in pool.machines if m.local), None)

    def tally(c, slug: str) -> None:
        for path, kind, assignee in c.execute(
                "SELECT workspace_path, workspace_kind, assignee FROM tasks WHERE status = 'running'"):
            if is_remote(path):
                machine = pool.machine_for(path)
                if machine is not None:
                    counts[machine.name] += 1
            elif local is not None and kind == "worktree" and pool.serves(slug, assignee):
                counts[local.name] += 1

    tally(conn, board or kb.DEFAULT_BOARD)
    try:
        current = str(kb.kanban_db_path(board=board).expanduser().resolve())
        boards = kb.list_boards(include_archived=False)
    except Exception:
        return counts
    for meta in boards:
        slug = meta.get("slug") or kb.DEFAULT_BOARD
        try:
            path = kb.kanban_db_path(board=slug).expanduser()
            if str(path.resolve()) == current or not path.exists():
                continue
            other = kbc.connect(board=slug)
            try:
                tally(other, slug)
            finally:
                with contextlib.suppress(Exception):
                    other.close()
        except Exception:
            continue
    return counts


def choose_machine(pool: Pool, conn, board: Optional[str], *, prefer: Optional[Machine] = None,
                   probe: Callable[[Pool, Machine], Optional[str]] = None) -> Optional[tuple[Machine, str]]:
    """``(machine, remote_home)`` for the next task, or None when every machine is full or
    unreachable. ``prefer`` is the machine a retried task last ran on, where its worktree and any
    unpushed commits are: it wins when it has a slot, and while it is reachable but full the task
    waits for it rather than restarting elsewhere. Otherwise the lowest load (running /
    max_in_progress), then fewest running, then name."""
    probe = probe or remote_home
    running = running_by_machine(pool, conn, board)
    free = [m for m in pool.machines if running[m.name] < m.max_in_progress]
    if (prefer is not None and prefer not in free and prefer in pool.machines
            and (prefer.local or probe(pool, prefer))):
        return None
    free.sort(key=lambda m: (m != prefer, running[m.name] / m.max_in_progress, running[m.name], m.name))
    for machine in free:
        home = "" if machine.local else probe(pool, machine)
        if machine.local or home:
            return machine, home
    return None


# --- remote worktrees ----------------------------------------------------------------------

_NEWEST_TRUNK = r'''
base=HEAD
for n in main master; do
  hl=0; hr=0
  git rev-parse --verify --quiet "refs/heads/$n" >/dev/null && hl=1
  git rev-parse --verify --quiet "refs/remotes/origin/$n" >/dev/null && hr=1
  if [ $hl = 1 ] && [ $hr = 1 ]; then
    if git merge-base --is-ancestor "refs/heads/$n" "refs/remotes/origin/$n"; then base="refs/remotes/origin/$n"; else base="refs/heads/$n"; fi
    break
  elif [ $hl = 1 ]; then base="refs/heads/$n"; break
  elif [ $hr = 1 ]; then base="refs/remotes/origin/$n"; break
  fi
done
'''


def prepare_script(repo: str, origin: str, task_id: str, branch: str) -> str:
    """Bash that clones ``origin`` into ``repo`` on first use, then materializes
    ``<repo>/.worktrees/<task_id>`` on ``branch`` (existing local branch, else the pushed
    ``origin/<branch>`` a parent task left, else a new branch from the newest trunk — the same
    order as the local dispatcher). Prints the worktree path."""
    q = shlex.quote
    target = f"{repo}/.worktrees/{task_id}"
    return f'''set -e
repo={q(repo)}; target={q(target)}; branch={q(branch)}
if [ ! -d "$repo/.git" ]; then mkdir -p "$(dirname "$repo")"; git clone --quiet {q(origin)} "$repo"; fi
cd "$repo"
grep -qxF '.worktrees/' .git/info/exclude 2>/dev/null || echo '.worktrees/' >> .git/info/exclude
git fetch --quiet origin || true
if [ -d "$target" ]; then
  cur=$(git -C "$target" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  if [ "$cur" = "$branch" ]; then printf '%s\\n' "$target"; exit 0; fi
  echo "worktree $target is on branch $cur, expected $branch" >&2; exit 3
fi
mkdir -p "$repo/.worktrees"
if git show-ref --verify --quiet "refs/heads/$branch"; then
  git worktree add --quiet "$target" "$branch"
elif git show-ref --verify --quiet "refs/remotes/origin/$branch"; then
  git worktree add --quiet -b "$branch" "$target" "refs/remotes/origin/$branch"
else
{_NEWEST_TRUNK}
  git worktree add --quiet -b "$branch" "$target" "$base"
fi
printf '%s\\n' "$target"
'''


def cleanup_script(repo: str, target: str, branch: str) -> str:
    """Bash that removes ``target`` only when it holds no work: a clean tree and every commit
    already in ``origin/main`` (or master) by patch — the local dispatcher's predicate. Any
    doubt keeps it. Auto-named ``wt/`` branches go with it."""
    q = shlex.quote
    return f'''repo={q(repo)}; target={q(target)}; branch={q(branch)}
[ -d "$target" ] || {{ echo gone; exit 0; }}
cd "$repo" || exit 0
[ -z "$(git -C "$target" status --porcelain 2>/dev/null)" ] || {{ echo dirty; exit 0; }}
git fetch --quiet origin || {{ echo nofetch; exit 0; }}
ref=""
for n in main master; do
  if git rev-parse --verify --quiet "refs/remotes/origin/$n" >/dev/null; then ref="refs/remotes/origin/$n"; break; fi
done
[ -n "$ref" ] || {{ echo notrunk; exit 0; }}
out=$(git -C "$target" cherry "$ref" HEAD) || {{ echo nocherry; exit 0; }}
if printf '%s\\n' "$out" | grep -q '^+'; then echo unmerged; exit 0; fi
git worktree remove "$target" || {{ echo removefailed; exit 0; }}
case "$branch" in wt/*) git branch -D "$branch" >/dev/null 2>&1 || true;; esac
echo removed
'''


def local_repo_for(task, board: Optional[str]) -> Optional[Path]:
    """The board ``default_workdir`` repo a task may run through the pool for, or None. Only
    tasks on that repo qualify (workspace unset, the repo root, already remote, or this task's own
    local ``.worktrees/<id>``), so a retry on another machine can always find the origin to clone."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_workspace import _git_toplevel, _path_key

    if (task.workspace_kind or "scratch") != "worktree":
        return None
    slug = board or kb.get_current_board()
    default = (kb.read_board_metadata(slug).get("default_workdir") or "").strip()
    if not default or not os.path.isabs(os.path.expanduser(default)):
        return None
    root = _git_toplevel(Path(default).expanduser())
    if root is None:
        return None
    path = task.workspace_path
    if not path or is_remote(path):
        return root
    key = _path_key(Path(path).expanduser().resolve(strict=False))
    return root if key in (_path_key(root), _path_key(root / ".worktrees" / task.id)) else None


def prior_machine(pool: Pool, task) -> Optional[Machine]:
    """The machine holding a retried task's worktree: its URI's machine, or the local entry for a
    local ``.worktrees/<id>`` checkout. None for a task that has not run yet."""
    path = task.workspace_path
    if is_remote(path):
        return pool.machine_for(path)
    if path and Path(path).name == task.id and Path(path).parent.name == ".worktrees":
        return next((m for m in pool.machines if m.local), None)
    return None


def origin_url(repo_root: Path) -> str:
    from hermes_cli.kanban_db_workspace import _git
    result = _git(repo_root, "remote", "get-url", "origin", timeout=10)
    url = (result.stdout or "").strip()
    if result.returncode != 0 or not url:
        raise ExecutorError(f"{repo_root} has no origin remote to clone from")
    return url


def prepare_worktree(pool: Pool, machine: Machine, home: str, repo_root: Path, task_id: str,
                     branch: str, *, runner: Callable[..., subprocess.CompletedProcess] = None) -> str:
    """Materialize the task's worktree on ``machine``; returns its ``ssh://`` URI."""
    repo = f"{machine_workdir(machine, home)}/{repo_root.name}"
    result = (runner or run_script)(pool, machine, prepare_script(repo, origin_url(repo_root), task_id, branch),
                                    timeout=600)
    lines = (result.stdout or "").strip().splitlines()
    if result.returncode != 0 or not lines or not lines[-1].startswith("/"):
        raise ExecutorError(f"{machine.name}: worktree setup failed: {(result.stderr or result.stdout or '').strip()}")
    return make_uri(machine, lines[-1].strip())


def cleanup_worktree(task_id: str, uri: str, branch_name: Optional[str], *, pool: Optional[Pool] = None,
                     runner: Callable[..., subprocess.CompletedProcess] = None) -> Optional[str]:
    """Best-effort removal of a finished task's remote worktree; returns the script's verdict
    (``removed`` / ``dirty`` / ``unmerged`` / ...) or None when it could not run."""
    pool = pool if pool is not None else load_pool()
    machine = pool.machine_for(uri) if pool else None
    if machine is None:
        _log.info("Preserving remote worktree for task %s: %s is not in the executor pool", task_id, uri)
        return None
    target = remote_path(uri)
    parts = target.rstrip("/").split("/")
    if len(parts) < 3 or parts[-2] != ".worktrees":
        return None  # not a <repo>/.worktrees/<id> checkout — never guess
    repo = "/".join(parts[:-2])
    branch = (branch_name or "").strip() or f"wt/{task_id}"
    try:
        result = (runner or run_script)(pool, machine, cleanup_script(repo, target, branch), timeout=180)
    except ExecutorError as exc:
        _log.info("Preserving remote worktree for task %s: %s", task_id, exc)
        return None
    verdict = (result.stdout or "").strip().splitlines()[-1:] or [""]
    if verdict[0] != "removed":
        _log.info("Preserving remote worktree for task %s at %s: %s", task_id, uri, verdict[0])
    return verdict[0]


def terminal_overrides(pool: Pool, uri: str) -> dict[str, str]:
    """The worker session's terminal scope for a remote workspace: Hermes' SSH backend on the
    task's machine, working in its worktree, without copying this host's skills or credential
    files to that machine."""
    machine = pool.machine_for(uri)
    if machine is None:
        raise ExecutorError(f"{uri} is not in kanban.executor_pool")
    return {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": machine.host, "TERMINAL_SSH_USER": machine.user,
            "TERMINAL_SSH_PORT": str(machine.port), "TERMINAL_SSH_KEY": pool.key,
            "TERMINAL_SSH_SYNC_FILES": "false", "TERMINAL_CWD": remote_path(uri)}
