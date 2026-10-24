#!/usr/bin/env python3
"""Deterministic local discovery/task journal. No network, scheduling or model calls."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from dev_progress_board import TZ, atomic_json, digest
import dev_progress_review as review

STATES = {"unverified": "待核实", "ready": "待分派", "executing": "执行中",
          "awaiting_review": "待验收", "closed": "关闭", "duplicate": "重复", "deferred": "暂不处理"}
INGRESS = {"discover", "inspection", "receipt"}
PAYLOAD_FIELDS = {
    "discover": "source source_ref project fingerprint title severity observed_at",
    "inspection": "source_ref project observed_at summary",
    "issue_state": "issue_id state reason duplicate_of",
    "task": "id source project title objective scope acceptance approval_ref issue_ids",
    "link": "issue_id task_id",
    "window": "task_id kind profile session_id executor binding url",
    "receipt": "task_id window_id kind progress next_step blocker",
    "review": "task_id approval_ref checks",
    "close": "task_id approval_ref reason outcome",
    "reopen": "task_id approval_ref reason",
}


def entity_id(prefix, payload, keys):
    return prefix + "-" + digest({key: payload[key] for key in keys})[:20]


def text(obj, key, limit=500):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key}必须是非空文本且不超过{limit}字符")
    return value


def identifier(obj, key):
    value = text(obj, key, 100)
    if not re.fullmatch(r"[\w.-]+", value) or value in {".", ".."}:
        raise ValueError(f"{key}无效")
    return value


def timestamp(obj):
    value = text(obj, "observed_at", 50)
    # Deployment also supports macOS system Python 3.9, which predates Z support.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    if datetime.fromisoformat(normalized).tzinfo is None:
        raise ValueError("observed_at必须包含时区")


def evidence(obj):
    refs = obj.get("evidence")
    if not isinstance(refs, list) or not refs or len(refs) > 10:
        raise ValueError("必须提供1至10项原始证据引用")
    for ref in refs:
        text({"ref": ref}, "ref")


def window_url(value):
    # Links are navigation only, never destinations for requests by this script.
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "discord.com" or parsed.query or parsed.fragment:
        raise ValueError("执行窗口须为Discord HTTPS原生会话链接")
    if not re.fullmatch(r"/channels/[0-9]+/[0-9]+(?:/[0-9]+)?", parsed.path) or parsed.netloc != "discord.com":
        raise ValueError("执行窗口链接无效")


class Pool:
    def __init__(self, path):
        self.path = Path(path)
        obj = json.loads(self.path.read_text(encoding="utf-8-sig")) if self.path.exists() else {"version": 1, "events": []}
        if not isinstance(obj, dict) or obj.get("version") != 1 or not isinstance(obj.get("events"), list):
            raise ValueError("问题任务池格式无效")
        self.events, self.head = [], review.ZERO
        self.data = {"issues": {}, "tasks": {}, "windows": {}, "inspections": []}
        for event in obj["events"]:
            if not isinstance(event, dict) or set(event) != {"sequence", "previous", "hash", "at", "request", "hwb_sources"}:
                raise ValueError("问题任务池事件格式无效")
            body = {k: v for k, v in event.items() if k != "hash"}
            if event.get("sequence") != len(self.events) + 1 or event.get("previous") != self.head or event.get("hash") != digest(body):
                raise ValueError("问题任务池哈希链损坏")
            self.validate_request(event["request"])
            if any(e["request"]["request_id"] == event["request"]["request_id"] for e in self.events):
                raise ValueError("请求编号重复")
            self.reduce(event)
            self.events.append(event)
            self.head = event["hash"]

    def validate_request(self, request):
        if not isinstance(request, dict) or set(request) != {"request_id", "action", "actor", "evidence", "payload"}:
            raise ValueError("请求字段必须为request_id/action/actor/evidence/payload")
        identifier(request, "request_id")
        text(request, "actor", 100)
        evidence(request)
        if request["action"] not in HANDLERS or not isinstance(request["payload"], dict):
            raise ValueError("操作或payload无效")
        if set(request["payload"]) - set(PAYLOAD_FIELDS[request["action"]].split()):
            raise ValueError("payload包含未知字段")

    def reduce(self, event):
        request = event["request"]
        p = request["payload"]
        if request["action"] in {"task", "review", "close", "reopen"}:
            tid = p.get("id") if request["action"] == "task" else p.get("task_id")
            for old in self.events:
                previous = old["request"]["payload"]
                old_tid = previous.get("id") if old["request"]["action"] == "task" else previous.get("task_id")
                if old_tid == tid and previous.get("approval_ref") == p.get("approval_ref"):
                    raise ValueError("新一轮操作必须使用新的明确审批引用")
        # Projection lists must never alias the append-only journal payload.
        HANDLERS[request["action"]](self, copy.deepcopy(request["payload"]), event)

    def validate_anchor(self, state):
        head = state.get("pool_head")
        if head and head not in {review.ZERO, *(e["hash"] for e in self.events)}:
            raise ValueError("已投递问题任务池丢失或回退；必须恢复备份")

    def apply(self, home, request):
        import fcntl

        self.validate_request(request)
        if request["action"] not in INGRESS:
            review.require_controller(home)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Same lock as sync: no close can race with a delivery snapshot.
        with (self.path.parent / "sync.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            loaded = Pool(self.path)
            state_path = self.path.parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {}
            loaded.validate_anchor(state)
            for event in loaded.events:
                if event["request"]["request_id"] == request["request_id"]:
                    if event["request"] != request:
                        raise ValueError("请求编号已使用且内容不同")
                    self.__dict__.update(loaded.__dict__)
                    return event
            sources = loaded.preflight(home, request)
            event = {"sequence": len(loaded.events) + 1, "previous": loaded.head,
                     "at": datetime.now(TZ).isoformat(), "request": copy.deepcopy(request), "hwb_sources": sources}
            event["hash"] = digest(event)
            loaded.reduce(event)
            if request["action"] == "close" and loaded.preflight(home, request) != sources:
                raise ValueError("执行材料在关单提交前变化，请重新核验")
            atomic_json(self.path, {"version": 1, "events": loaded.events + [event]})
            loaded.events.append(event)
            loaded.head = event["hash"]
            self.__dict__.update(loaded.__dict__)
            return event

    def preflight(self, home, request):
        p = request["payload"]
        if request["action"] == "window" and p.get("kind") == "hwb":
            meta, _ = review.worker(home, identifier(p, "session_id"))
            if p.get("binding") != review.binding(meta):
                raise ValueError("HWB执行窗口必须绑定当前真实登记身份")
        if request["action"] not in {"review", "close"}:
            return {}
        task = self.data["tasks"][p["task_id"]]
        control = review.ReviewLog(self.path.parent / "control.json")
        rows = {t["id"]: t for t in review.project_tasks(review.scan_tasks(home), control)}
        sources = {}
        for wid in task["window_ids"]:
            window = self.data["windows"][wid]
            if window["kind"] != "hwb":
                continue
            row = rows.get(window["session_id"])
            sources[wid] = {"source_hashes": review.source_hashes(home, window["session_id"]),
                            "control_head": row.get("control_head") if row else None}
            if request["action"] == "close" and (not row or row.get("task_binding") != window["binding"] or not row.get("closed") or row["status"] not in {"已完成", "已取消"}):
                raise ValueError("关联HWB窗口须先满足原总控验收、关单与生命周期契约")
        return sources

    def work_hash(self, tid):
        task = self.data["tasks"][tid]
        return digest({"task": {k: v for k, v in task.items() if k not in {"review", "closed", "updated", "outcome"}},
                       "windows": {wid: self.data["windows"][wid] for wid in task["window_ids"]}})

    def task_status(self, tid):
        task = self.data["tasks"][tid]
        if task["closed"]:
            return "closed"
        receipts = list(task["receipts"].values())
        if receipts and len(receipts) == len(task["window_ids"]) and all(r["kind"] == "report" for r in receipts):
            return "awaiting_review"
        return "executing" if receipts else "ready"

    def issue_status(self, iid):
        issue = self.data["issues"][iid]
        if issue["state"] != "ready":
            return issue["state"]
        statuses = [self.task_status(tid) for tid in issue["task_ids"]]
        if statuses and all(s in {"awaiting_review", "closed"} for s in statuses):
            return "awaiting_review"
        return "executing" if "executing" in statuses else "ready"

    def counts(self, hwb_tasks):
        hwb = {w["session_id"] for w in self.data["windows"].values() if w["kind"] == "hwb"}
        legacy = {t["id"] for t in hwb_tasks}
        return {"issues": len(self.data["issues"]), "tasks": len(self.data["tasks"]) + len(legacy - hwb),
                "windows": len(self.data["windows"]) + len(legacy - hwb)}

    def snapshot(self):
        data = copy.deepcopy(self.data)
        for iid, issue in data["issues"].items():
            issue["status"] = self.issue_status(iid)
        for tid, task in data["tasks"].items():
            task["status"] = self.task_status(tid)
        return data


def discover(log, p, event):
    if p.get("source") != "ops_inspection":
        raise ValueError("问题发现来源必须为ops_inspection；目标使用task操作")
    for key in ("source_ref", "project", "fingerprint", "title"):
        text(p, key)
    timestamp(p)
    if p.get("severity") not in {"P0", "P1", "P2", "P3"}:
        raise ValueError("severity须为P0/P1/P2/P3")
    iid = entity_id("issue", p, ("source", "project", "fingerprint"))
    issue = log.data["issues"].setdefault(iid, dict(p, id=iid, state="unverified", task_ids=[], observations=[], reason="尚未经总控核实"))
    observation = {"actor": event["request"]["actor"], "evidence": event["request"]["evidence"], **p}
    for old in issue["observations"]:
        if old["source_ref"] == p["source_ref"]:
            if {k: v for k, v in old.items() if k != "at"} != observation:
                raise ValueError("同一次巡检观察冲突，请使用新的source_ref保留原证据")
            return
    issue["observations"].append({"at": event["at"], **observation})
    issue["updated"] = event["at"]


def inspection(log, p, event):
    for key in ("source_ref", "project", "summary"):
        text(p, key)
    timestamp(p)
    run = {**p, "actor": event["request"]["actor"], "evidence": event["request"]["evidence"]}
    for old in log.data["inspections"]:
        if (old["source_ref"], old["project"]) == (p["source_ref"], p["project"]):
            if {k: v for k, v in old.items() if k != "at"} != run:
                raise ValueError("同一次巡检回传冲突，请使用新的source_ref")
            return
    log.data["inspections"].append({**run, "at": event["at"]})


def issue_state(log, p, event):
    issue = log.data["issues"][identifier(p, "issue_id")]
    text(p, "reason")
    state = p.get("state")
    if state not in {"unverified", "ready", "duplicate", "deferred", "closed"}:
        raise ValueError("问题状态须由证据和任务进度决定")
    if state == "duplicate":
        target = log.data["issues"][identifier(p, "duplicate_of")]
        if target is issue or target["state"] == "duplicate":
            raise ValueError("重复必须指向非重复原问题，禁止循环")
        if any(i.get("duplicate_of") == issue["id"] for i in log.data["issues"].values()):
            raise ValueError("已有重复引用，不能建立重复链")
    if state in {"closed", "duplicate", "deferred"} and any(not log.data["tasks"][tid]["closed"] for tid in issue["task_ids"]):
        raise ValueError("仍有关联未关闭任务，不能隐藏执行中的问题")
    issue.pop("duplicate_of", None)
    if state == "duplicate":
        issue["duplicate_of"] = p["duplicate_of"]
    issue.update(state=state, reason=p["reason"], updated=event["at"])


def create_task(log, p, event):
    tid = identifier(p, "id")
    if tid in log.data["tasks"]:
        raise ValueError("任务已存在；请关联或回传而非重复创建")
    for key in ("project", "title", "objective", "scope", "acceptance", "approval_ref"):
        text(p, key)
    if p.get("source") not in {"ops_inspection", "approved_goal"}:
        raise ValueError("任务只接受ops_inspection或approved_goal来源")
    ids = p.get("issue_ids")
    if not isinstance(ids, list) or len(set(ids)) != len(ids):
        raise ValueError("issue_ids须为无重复列表")
    if p["source"] == "ops_inspection" and not ids:
        raise ValueError("运维任务必须关联已确认问题，含运维自处理")
    for iid in ids:
        if log.data["issues"][iid]["state"] != "ready":
            raise ValueError("问题先核实为ready，再创建执行任务")
    log.data["tasks"][tid] = {**p, "window_ids": [], "receipts": {}, "review": None,
                               "closed": False, "created": event["at"], "updated": event["at"]}
    for iid in ids:
        log.data["issues"][iid]["task_ids"].append(tid)


def link(log, p, event):
    issue = log.data["issues"][identifier(p, "issue_id")]
    task = log.data["tasks"][identifier(p, "task_id")]
    if task["closed"] or issue["state"] in {"closed", "duplicate", "deferred"}:
        raise ValueError("关联前先显式重开问题/任务")
    if issue["id"] not in task["issue_ids"]:
        task["issue_ids"].append(issue["id"])
        issue["task_ids"].append(task["id"])
    task["updated"] = event["at"]


def window(log, p, event):
    task = log.data["tasks"][identifier(p, "task_id")]
    if task["closed"]:
        raise ValueError("已关单，先reopen")
    for key in ("profile", "session_id", "executor"):
        identifier(p, key)
    if p.get("kind") not in {"native", "hwb"}:
        raise ValueError("执行窗口须为native或hwb")
    if p["kind"] == "native":
        window_url(text(p, "url"))
    elif not re.fullmatch(r"[0-9a-f]{64}", p.get("binding", "")):
        raise ValueError("缺少HWB登记binding")
    if p.get("url"):
        window_url(text(p, "url"))
    if p["kind"] == "hwb" and any(w["kind"] == "hwb" and w["session_id"] == p["session_id"] and w["profile"] != p["profile"]
                                 for w in log.data["windows"].values()):
        raise ValueError("同一HWB登记不允许用其他profile别名重复创建执行窗口")
    wid = entity_id("window", p, ("kind", "profile", "session_id"))
    value = {k: v for k, v in p.items() if k != "task_id"}
    existing = log.data["windows"].get(wid)
    if existing is not None and existing != value:
        raise ValueError("已登记窗口身份或链接冲突，禁止更换")
    log.data["windows"][wid] = value
    if wid not in task["window_ids"]:
        task["window_ids"].append(wid)
    task["updated"] = event["at"]


def receipt(log, p, event):
    task = log.data["tasks"][identifier(p, "task_id")]
    wid = identifier(p, "window_id")
    if task["closed"] or wid not in task["window_ids"]:
        raise ValueError("任务已关闭或窗口未绑定")
    if event["request"]["actor"] != log.data["windows"][wid]["executor"]:
        raise ValueError("回传actor必须与登记执行者一致；真实性另由总控读回")
    if p.get("kind") not in {"accepted", "report"}:
        raise ValueError("回传kind须为accepted或report")
    if p["kind"] == "report" and wid not in task["receipts"]:
        raise ValueError("未实际接单，不能跳过接单直接回传")
    for key in ("progress", "next_step"):
        text(p, key)
    if "blocker" in p:
        text(p, "blocker")
    task["receipts"][wid] = {**p, "at": event["at"], "evidence": event["request"]["evidence"], "event": event["hash"]}
    task["updated"] = event["at"]


def review_task(log, p, event):
    task = log.data["tasks"][identifier(p, "task_id")]
    text(p, "approval_ref")
    if task["closed"]:
        raise ValueError("先显式reopen再验收")
    review.validate_checks(p.get("checks"))
    task["review"] = {**p, "work_hash": log.work_hash(task["id"]), "event": event["hash"],
                       "actor": event["request"]["actor"], "evidence": event["request"]["evidence"],
                       "hwb_sources": event["hwb_sources"]}


def close_task(log, p, event):
    task = log.data["tasks"][identifier(p, "task_id")]
    text(p, "approval_ref")
    text(p, "reason")
    checked = task["review"]
    if p.get("outcome") not in {"completed", "cancelled"} or task["closed"] or not checked:
        raise ValueError("关单须有本轮三层验收和明确outcome")
    if any(c["status"] == "unverified" for c in checked["checks"].values()) or checked["work_hash"] != log.work_hash(task["id"]):
        raise ValueError("验收未满足或验收后源记录已变化")
    if checked["hwb_sources"] != event["hwb_sources"]:
        raise ValueError("验收后HWB原始材料或总控记录已变化，必须重新核验")
    if p["outcome"] == "completed" and log.task_status(task["id"]) != "awaiting_review":
        raise ValueError("完成关单必须有全部执行窗口接单及回传证据")
    task.update(closed=True, outcome=p["outcome"], updated=event["at"], close_event=event["hash"])


def reopen_task(log, p, event):
    task = log.data["tasks"][identifier(p, "task_id")]
    text(p, "approval_ref")
    text(p, "reason")
    if not task["closed"]:
        raise ValueError("任务尚未关闭")
    old_refs = {e["request"]["payload"].get("approval_ref") for e in log.events
                if e["request"]["payload"].get("task_id") == task["id"]}
    if p["approval_ref"] in old_refs:
        raise ValueError("重新开启必须使用新审批引用")
    task.update(closed=False, review=None, receipts={}, updated=event["at"])
    task.pop("close_event", None)
    task.pop("outcome", None)


HANDLERS = {"discover": discover, "inspection": inspection, "issue_state": issue_state,
            "task": create_task, "link": link, "window": window, "receipt": receipt,
            "review": review_task, "close": close_task, "reopen": reopen_task}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot", help="只读持久化问题/任务/执行窗口")
    commands.add_parser("verify", help="只读校验哈希链及投递anchor")
    sub = commands.add_parser("apply", help="原子登记一个JSON请求；无网络调用")
    sub.add_argument("--file", required=True, type=Path)
    args = parser.parse_args()
    log = Pool(args.state_dir / "pool.json")
    if args.command == "apply":
        request = json.loads(args.file.read_text(encoding="utf-8-sig"))
        event = log.apply(args.home, request)
        result = {"head": event["hash"], "request_id": request["request_id"]}
        if request["action"] == "discover":
            result["issue_id"] = entity_id("issue", request["payload"], ("source", "project", "fingerprint"))
        if request["action"] == "window":
            result["window_id"] = entity_id("window", request["payload"], ("kind", "profile", "session_id"))
        print(json.dumps(result))
        return
    state = args.state_dir / "state.json"
    log.validate_anchor(json.loads(state.read_text(encoding="utf-8-sig")) if state.exists() else {})
    output = log.snapshot() if args.command == "snapshot" else {"head": log.head, "events": len(log.events), "counts": log.counts([])}
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never echo user payloads, filesystem paths or secret-bearing exceptions.
        print(f"问题任务池操作失败：{type(exc).__name__}；核对协议、权限或恢复备份", file=sys.stderr)
        raise SystemExit(1) from None
