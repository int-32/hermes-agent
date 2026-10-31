"""Board execution policy and external observations, without native attempt records.

The policy is in the board's own SQLite DB. Claims read it under their writer
transaction; a mode switch is allowed only while the board is empty. There is
no process cache or status/assignee heuristic to lose on a service restart.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def board_tracking_only(conn: sqlite3.Connection) -> bool:
    row = conn.execute('SELECT tracking_only FROM kanban_board_policy WHERE id=1').fetchone()
    if row is None or type(row[0]) is not int or row[0] not in (0, 1):
        raise ValueError('board execution policy is missing or invalid')
    return bool(row[0])


def set_board_tracking_only(conn: sqlite3.Connection, tracking_only: bool) -> None:
    if type(tracking_only) is not bool:
        raise ValueError('tracking_only must be a boolean')
    from hermes_cli import kanban_db_connect as kbc
    path = kbc._main_db_file(conn)
    if not path:
        raise ValueError('file-backed board required')
    with kbc._dispatch_tick_lock(Path(path)) as held:
        if not held:
            raise ValueError('dispatcher tick in progress; retry the empty-board mode change')
        _set_empty_board_mode(conn, tracking_only)


def _set_empty_board_mode(conn, tracking_only):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        if board_tracking_only(conn) == tracking_only:
            return
        # The same IMMEDIATE lock serializes this decision with native claims.
        # An occupied board cannot change meaning below a queued/running worker.
        if conn.execute('SELECT 1 FROM tasks LIMIT 1').fetchone() or conn.execute('SELECT 1 FROM task_runs LIMIT 1').fetchone():
            raise ValueError('execution mode can change only on an empty board')
        conn.execute('UPDATE kanban_board_policy SET tracking_only=? WHERE id=1', (int(tracking_only),))


def observe_tracking_task(conn: sqlite3.Connection, task_id: str, status: str, *, actor: str,
                          evidence: dict, expected_status: str | None = None) -> bool:
    """Record an external lifecycle observation in native columns, never a run.

    Composes in an outer receipt transaction. Explicit acceptance is required
    for done; a persisted native PR contract cannot be satisfied by observation
    data. Ordinary native completion remains the only path for that contract.
    """
    from hermes_cli import kanban_db as kb

    if status not in kb.VALID_STATUSES or not actor or not isinstance(evidence, dict):
        raise ValueError('valid tracking status, actor and evidence required')
    with kb.write_txn(conn, allow_nested=True):
        if not board_tracking_only(conn):
            raise ValueError('external status observations require a tracking-only board')
        task = kb.get_task(conn, task_id)
        if task is None or (expected_status is not None and task.status != expected_status):
            return False
        if any((task.claim_lock, task.current_run_id, task.worker_pid)):
            raise ValueError('native execution ownership cannot become an external observation')
        if status in {'ready', 'running', 'review', 'done'} and not kb._parents_satisfied(conn, task_id):
            return False
        if status == 'done':
            if evidence.get('goal_status') != 'accepted':
                raise ValueError('accepted goal evidence required for external completion')
            if task.completion_contract not in (None, 'local-only'):
                raise ValueError('external observations cannot satisfy the native PR acceptance contract')
            kb._gate_empty_completion(conn, task_id, result=evidence.get('result'), summary=None)
        if task.status == status:
            return True
        completed = int(time.time()) if status == 'done' else None
        conn.execute('UPDATE tasks SET status=?, completed_at=?, result=COALESCE(?, result) WHERE id=?',
                     (status, completed, evidence.get('result') if status == 'done' else None, task_id))
        kb._append_event(conn, task_id, 'tracking_status', {'from': task.status, 'status': status,
                         'actor': actor, 'evidence': evidence})
    return True


def read_tracking_metadata(path: Path) -> bool:
    """Read display policy without initializing a missing or legacy board."""
    if not path.is_file():
        return False
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.execute('PRAGMA query_only=ON')
        if not conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='kanban_board_policy'").fetchone():
            return False
        return board_tracking_only(conn)
