#!/usr/bin/env python3
"""Controller-only local acceptance ledger; workers may export candidates, never sign."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess

from dev_progress_board import TZ, alive, atomic_json, digest, scan_tasks

STAGES = {"development": "开发完成", "merge": "合入主线", "release": "上线验收"}
LABELS = {"passed": "已核验", "not_applicable": "不适用", "unverified": "未核验"}
ZERO = "0" * 64
ACTIVE = {"running", "starting", "waiting_decision"}


def ancestry():
    """Exact PID/PPID metadata only; never infer worker identity from argv."""
    pid, result = os.getpid(), set()
    while pid > 1:
        if pid in result or len(result) >= 128:
            raise PermissionError("无法核验控制进程的祖先链")
        result.add(pid)
        row = subprocess.run(["ps", "-o", "pid=,ppid=", "-p", str(pid)],
                             check=True, capture_output=True, text=True, timeout=5).stdout.split()
        if len(row) != 2 or int(row[0]) != pid:
            raise PermissionError("无法核验控制进程的PID")
        pid = int(row[1])
    return result


def require_controller(home):
    if any(os.environ.get(key) for key in ("CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "HERMES_KANBAN_TASK")):
        raise PermissionError("worker只能导出候选，禁止签署自己或历史任务；请总控在worker外执行")
    parents = ancestry()
    for path in (home / "workers").glob("*/meta.json"):
        meta = json.loads(path.read_text(encoding="utf-8-sig"))
        if meta.get("pid") in parents:
            raise PermissionError("HWB worker祖先链内禁止签署；请总控在worker外执行")


def validate_checks(checks):
    if not isinstance(checks, dict) or set(checks) != set(STAGES):
        raise ValueError("必须分别记录development、merge、release三层验收")
    for key, check in checks.items():
        if not isinstance(check, dict):
            raise ValueError(f"{key}验收项必须是对象")
        if check.get("status") not in LABELS:
            raise ValueError(f"{key}验收状态无效")
        if "reason" in check and not isinstance(check["reason"], str):
            raise ValueError(f"{key}验收原因必须是文字")
        evidence = check.get("evidence", [])
        if not isinstance(evidence, list) or any(not isinstance(ref, str) or not ref.strip() for ref in evidence):
            raise ValueError(f"{key}证据必须是非空引用列表")
        if check["status"] == "passed" and not evidence:
            raise ValueError(f"{key}已核验必须有原始证据引用")
        if check["status"] == "not_applicable" and not check.get("reason", "").strip():
            raise ValueError(f"{key}不适用必须说明理由")


def validate_reason(payload):
    if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
        raise ValueError("必须记录原因")
    if not isinstance(payload.get("evidence"), list) or not payload["evidence"] or any(not isinstance(x, str) or not x.strip() for x in payload["evidence"]):
        raise ValueError("必须提供明确证据引用")


def worker(home, tid):
    if Path(tid).name != tid or tid in {".", ".."}:
        raise ValueError("任务ID无效")
    directory = home / "workers" / tid
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8-sig"))
    if meta.get("id") != tid or meta.get("tool") not in {"claude", "codex"}:
        raise ValueError("不是已登记HWB任务")
    if meta.get("pid") is not None and (not isinstance(meta["pid"], int) or meta["pid"] <= 0):
        raise ValueError("HWB登记PID无效，禁止推断进程已停止")
    pending = sorted(p.stem for p in (directory / "decisions").glob("*.json")
                     if p.stem.isdigit() and not p.with_name(p.stem + ".answer.json").exists())
    return meta, {"status": meta.get("status"), "pid": meta.get("pid"),
                  "alive": alive(meta.get("pid")), "pending": pending}


def binding(meta):
    return digest({key: meta.get(key) for key in ("id", "created", "tool", "cwd")})


def source_hashes(home, tid):
    result = {}
    for name in ("meta.json", "events.jsonl"):
        path = home / "workers" / tid / name
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


def close_guard(life):
    if not isinstance(life, dict) or not isinstance(life.get("pending"), list) or not isinstance(life.get("alive"), bool):
        raise ValueError("worker生命周期证据无效")
    if life["pending"]:
        raise ValueError("存在pending，先由总控处理决策，再hwb stop，不能静默关单")
    if life["alive"] or life["status"] in ACTIVE:
        raise ValueError("worker仍活动；请总控明确执行hwb stop <任务ID>并读回后再close")
    if life["status"] not in {"idle", "stopped", "failed", "dead"}:
        raise ValueError("worker生命周期未核验，不能关单")


class ReviewLog:
    def __init__(self, path):
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"version": 1, "events": []}
        if data.get("version") != 1 or not isinstance(data.get("events"), list):
            raise ValueError("验收记录格式无效")
        self.events = []
        previous = ZERO
        for index, event in enumerate(data["events"], 1):
            if not isinstance(event, dict):
                raise ValueError("验收事件格式无效")
            body = {k: v for k, v in event.items() if k != "hash"}
            if event.get("sequence") != index or event.get("previous") != previous or event.get("hash") != digest(body):
                raise ValueError("验收记录链损坏，停止而非降级为未验收")
            if not event.get("controller") or not event.get("approval_ref") or event.get("action") not in {"review", "close", "reopen"}:
                raise ValueError("缺少总控身份、审批引用或有效操作")
            sources = event.get("source_hashes")
            if not isinstance(sources, dict) or set(sources) != {"meta.json", "events.jsonl"} or not sources.get("meta.json"):
                raise ValueError("缺少验收时原始材料的哈希")
            if event["action"] == "review":
                validate_checks(event["payload"]["checks"])
                if self.current(event["task_id"])["close"]:
                    raise ValueError("关闭后未reopen却出现新验收")
            else:
                validate_reason(event["payload"])
            if event["action"] == "close":
                prior = self.current(event["task_id"])
                if prior["close"] or not prior["review"] or event["payload"].get("review_hash") != prior["review"]["hash"]:
                    raise ValueError("关闭记录未关联有效的本轮验收")
                if event.get("source_hashes") != prior["review"].get("source_hashes"):
                    raise ValueError("验收后源记录已变化，关闭记录无效")
                if event["payload"].get("outcome") not in {"completed", "cancelled"} or any(c["status"] not in {"passed", "not_applicable"} for c in prior["review"]["payload"]["checks"].values()):
                    raise ValueError("关闭记录的适用验收项未满足")
                close_guard(event["payload"]["worker_at_close"])
            if any(e["binding"] != event["binding"] for e in self.events if e["task_id"] == event["task_id"]):
                raise ValueError("同任务验收链的登记身份不一致")
            self.events.append(event)
            previous = event["hash"]
        self.head = previous

    def current(self, tid):
        state = {"review": None, "close": None, "reopen": None, "latest": None}
        for event in self.events:
            if event["task_id"] != tid:
                continue
            state["latest"] = event
            if event["action"] == "reopen":
                state.update(review=None, close=None, reopen=event)
            else:
                state[event["action"]] = event
        return state

    def validate_action(self, state, action, payload, life, board_state, tid, current_hashes):
        if action == "review":
            if state["close"]:
                raise ValueError("已关单，先显式reopen再重新验收")
            validate_checks(payload.get("checks"))
            return
        validate_reason(payload)
        if action == "reopen":
            return
        if action != "close" or payload.get("outcome") not in {"completed", "cancelled"}:
            raise ValueError("关单必须明确completed或cancelled")
        if state["close"]:
            raise ValueError("已有关闭记录，修改需先reopen")
        close_guard(life)
        if not state["review"] or any(c["status"] not in {"passed", "not_applicable"} for c in state["review"]["payload"]["checks"].values()):
            raise ValueError("所有适用验收项通过后才能关单")
        if state["review"].get("source_hashes") != current_hashes:
            raise ValueError("验收后源记录已变化；请停止worker后重新核验并使用新的总控审批")
        entry = board_state.get("tasks", {}).get(tid, {})
        if not entry.get("thread_id") or not entry.get("message_id"):
            raise ValueError("缺少原卡/thread映射，先同步并读回")
        if entry.get("sync_error"):
            raise ValueError("原thread存在同步阻塞；先由总控显式reopen并同步，再重新验收/关单")
        payload.update(review_hash=state["review"]["hash"], worker_at_close=life)

    def apply(self, home, tid, action, payload, *, controller, approval_ref, board_state):
        require_controller(home)
        if not controller.strip() or not approval_ref.strip():
            raise ValueError("必须明确总控身份和本次审批引用")
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            loaded = ReviewLog(self.path)
            self.events, self.head = loaded.events, loaded.head
            meta, life = worker(home, tid)
            identity = binding(meta)
            if any(e["binding"] != identity for e in self.events if e["task_id"] == tid):
                raise ValueError("任务登记身份已变化，禁止复用历史验收")
            state = self.current(tid)
            for event in self.events:
                if (event["task_id"], event["action"], event["approval_ref"]) == (tid, action, approval_ref):
                    original = {k: v for k, v in event["payload"].items() if k not in {"review_hash", "worker_at_close"}}
                    if original == payload and state["latest"] == event and event["controller"] == controller:
                        return event
                    raise ValueError("审批引用已使用；新一轮操作必须使用新的明确审批")
            payload = json.loads(json.dumps(payload))
            current_hashes = source_hashes(home, tid)
            self.validate_action(state, action, payload, life, board_state, tid, current_hashes)
            snapshot = next(t for t in scan_tasks(home, task_id=tid) if t["id"] == tid)
            if action == "close":
                fresh_meta, fresh_life = worker(home, tid)
                close_guard(fresh_life)
                if binding(fresh_meta) != identity or source_hashes(home, tid) != current_hashes:
                    raise ValueError("验收后源记录在关单前变化，请总控重新核验")
            event = {"sequence": len(self.events) + 1, "previous": self.head, "task_id": tid,
                     "binding": identity, "action": action, "payload": payload, "controller": controller,
                     "approval_ref": approval_ref, "at": datetime.now(TZ).isoformat(), "snapshot": snapshot,
                     "source_hashes": current_hashes}
            event["hash"] = digest(event)
            atomic_json(self.path, {"version": 1, "events": self.events + [event]})
            self.events.append(event)
            self.head = event["hash"]
            return event


def auto_close(home, log, board_state):
    """Consume existing controller acceptance; never create or upgrade a review."""
    require_controller(home)
    loaded = ReviewLog(log.path)
    log.events, log.head = loaded.events, loaded.head
    validate_delivery_anchor(log, board_state)
    tids = {path.parent.name for path in (home / "workers").glob("*/meta.json")}
    tids.update(event["task_id"] for event in log.events)
    results = []
    for tid in sorted(tids):
        row = {"task_id": tid}
        try:
            state = log.current(tid)
            if state["close"]:
                row.update(status="already_closed", close_hash=state["close"]["hash"])
            else:
                worker(home, tid)  # Validate each source separately, including unreviewed tasks.
                accepted = state["review"]
                if not accepted:
                    raise ValueError("无总控三层验收记录；待总控核验")
                payload = {"outcome": "completed", "reason": "自动收尾：复用已有且仍有效的总控三层验收",
                           "evidence": ["control.json#" + accepted["hash"], accepted["approval_ref"]]}
                event = log.apply(home, tid, "close", payload, controller="auto-close",
                                  approval_ref="auto-close:" + accepted["hash"], board_state=board_state)
                row.update(status="closed", close_hash=event["hash"])
        except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError) as exc:
            row.update(status="failed", reason=str(exc))
        except ValueError as exc:
            row.update(status="blocked", reason=str(exc))
        results.append(row)
    board_state["last_auto_close"] = {"at": datetime.now(TZ).isoformat(), "tasks": results}
    return results


def acceptance_text(event):
    if not event:
        return "｜".join(label + "：未核验" for label in STAGES.values())
    lines = []
    for key, label in STAGES.items():
        check = event["payload"]["checks"][key]
        suffix = "；理由：" + check["reason"][:96] if check["status"] in {"not_applicable", "unverified"} and check.get("reason") else ""
        refs = "；证据：" + "、".join(check.get("evidence", []))[:120] if check.get("evidence") else ""
        lines.append(label + "：" + LABELS[check["status"]] + suffix + refs)
    return "｜".join(lines)


def project_tasks(tasks, log):
    rows = {task["id"]: dict(task) for task in tasks}
    for tid in {event["task_id"] for event in log.events}:
        closed = log.current(tid)["close"]
        if closed and tid not in rows:
            rows[tid] = dict(closed["snapshot"], hwb_status="源记录缺失", worker_alive=False, pending=[])
    result = []
    for tid, task in rows.items():
        state = log.current(tid)
        if state["latest"] and task.get("task_binding") and state["latest"]["binding"] != task["task_binding"]:
            raise ValueError("当前HWB登记身份与总控记录不一致，停止同步")
        task.update(closed=bool(state["close"]), control_head=state["latest"]["hash"] if state["latest"] else None,
                    control_event=state["latest"], reopen_event=state["reopen"], acceptance=acceptance_text(state["review"]))
        checks = state["review"]["payload"]["checks"] if state["review"] else {}
        task["disposition"] = "；".join(STAGES[key] + "：" + check.get("reason", "待总控核验")[:96]
                                         for key, check in checks.items() if check["status"] == "unverified")
        if not state["review"]:
            task["disposition"] = "尚无独立总控验收记录，待总控核验"
        if task["status"] in {"空闲待验收", "待验收"}:
            task["status"] = "待总控核验"
        if state["close"]:
            event = state["close"]
            method = "总控验收后自动收尾" if event["controller"] == "auto-close" else "总控显式关单"
            task.update(status="已完成" if event["payload"]["outcome"] == "completed" else "已取消",
                        phase=method + "；完成不等于全部已上线", progress="总控验收/关单记录已持久化",
                        updated=event["at"], evidence="control.json#" + event["hash"])
            if task.get("pending") or task.get("worker_alive") or task.get("hwb_status") in ACTIVE:
                task.update(status="待决策", phase="关闭后worker再次活动或有pending；保持归档，需总控明确reopen")
        result.append(task)
    return sorted(result, key=lambda t: t["id"])


def validate_delivery_anchor(log, board_state):
    hashes = {event["hash"] for event in log.events}
    for row in board_state.get("tasks", {}).values():
        for key in ("applied_control_head", "archived_control_head", "applied_reopen"):
            if row.get(key) and row[key] not in hashes:
                raise ValueError("独立验收记录缺失或回退；先恢复总控记录备份，禁止自动重开/代补验收")


def candidates(home, log, board_state):
    result = []
    for task in project_tasks(scan_tasks(home), log):
        tid = task["id"]
        state = log.current(tid)
        if (home / "workers" / tid / "meta.json").exists():
            meta, life = worker(home, tid)
        else:
            meta, life = {}, {"status": "source_missing", "pid": None, "alive": False, "pending": []}
        blockers = []
        try:
            log.validate_action(state, "close", {"outcome": "completed", "reason": "只读候选门禁检查",
                                "evidence": [str(log.path)]}, life, board_state, tid, source_hashes(home, tid))
        except ValueError as exc:
            blockers.append(str(exc))
        raw = []
        for name in ("meta.json", "events.jsonl"):
            path = home / "workers" / tid / name
            if path.exists():
                raw.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "note": "原文只读；meta.last_message及turn_done属于worker自评，不是总控验收"})
        result.append({"task_id": tid, "business_status": task["status"], "worker_lifecycle": life,
                       "checks": state["review"]["payload"]["checks"] if state["review"] else None,
                       "close_blockers": blockers, "raw_evidence": raw,
                       "task_binding": state["latest"]["binding"] if not meta else binding(meta),
                       "approval": "只读候选导出，不新增验收或关单",
                       "existing_control_head": state["latest"]["hash"] if state["latest"] else None})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("candidates", help="只读候选和原始证据路径，不生成验收记录")
    commands.add_parser("verify", help="校验独立验收记录的schema和哈希链")
    commands.add_parser("auto-close", help="总控/定时脚本消费已有有效验收，逐任务记录结果；不新增验收")
    for verb in ("record", "close", "reopen"):
        sub = commands.add_parser(verb)
        sub.add_argument("task_id")
        sub.add_argument("--controller", required=True)
        sub.add_argument("--approval-ref", required=True)
        if verb == "record":
            sub.add_argument("--checks-file", required=True, type=Path)
        else:
            sub.add_argument("--reason", required=True)
            sub.add_argument("--evidence", action="append", required=True)
        if verb == "close":
            sub.add_argument("--outcome", choices=["completed", "cancelled"], required=True)
    args = parser.parse_args()
    log = ReviewLog(args.state_dir / "control.json")
    state_path = args.state_dir / "state.json"
    board_state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {"tasks": {}, "messages": {}}
    validate_delivery_anchor(log, board_state)
    if args.command == "verify":
        delivery = []
        for tid in sorted({e["task_id"] for e in log.events}):
            current, row = log.current(tid), board_state.get("tasks", {}).get(tid, {})
            delivery.append({"task_id": tid, "closed": bool(current["close"]),
                             "control_head": current["latest"]["hash"],
                             "applied_head": row.get("applied_control_head"),
                             "archived_head": row.get("archived_control_head"),
                             "thread_id": row.get("thread_id"), "card_id": row.get("message_id"),
                             "sync_error": row.get("sync_error")})
        print(json.dumps({"valid": True, "events": len(log.events), "head": log.head,
                          "delivery": delivery, "last_sync": board_state.get("last_sync"),
                          "last_auto_close": board_state.get("last_auto_close")}, ensure_ascii=False, indent=2))
        return
    if args.command == "candidates":
        print(json.dumps(candidates(args.home, log, board_state), ensure_ascii=False, indent=2))
        return
    require_controller(args.home)
    import fcntl

    with (args.state_dir / "sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        board_state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {"tasks": {}, "messages": {}}
        validate_delivery_anchor(ReviewLog(log.path), board_state)
        if args.command == "auto-close":
            results = auto_close(args.home, log, board_state)
            atomic_json(state_path, board_state)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return
        if args.command == "record":
            payload = {"checks": json.loads(args.checks_file.read_text(encoding="utf-8-sig"))}
            action = "review"
        else:
            payload = {"reason": args.reason, "evidence": args.evidence}
            action = args.command
            if action == "close":
                payload["outcome"] = args.outcome
        event = log.apply(args.home, args.task_id, action, payload, controller=args.controller,
                          approval_ref=args.approval_ref, board_state=board_state)
        print(json.dumps({"recorded": event["action"], "task_id": event["task_id"], "hash": event["hash"],
                          "note": "本命令只写独立总控记录；看板脚本下一次同步更新原卡/总览并归档"}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from None
