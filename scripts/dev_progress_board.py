#!/usr/bin/env python3
"""HWB-only Discord board. Stdlib, no model calls; run from a no-agent cron.

Deployment directory is also the durable journal. No worker files are written.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ACCEPTANCE = "开发完成：未核验｜合入主线：未核验｜上线验收：未核验"
EVENT_NAMES = {"created": "任务已登记", "turn_start": "执行轮次开始",
               "message": "执行者有新反馈（内容未独立核验）", "tool": "工具调用记录已落盘",
               "turn_done": "执行轮次结束（不代表开发完成）", "decision": "出现待决策项",
               "answered": "总控已答复决策", "error": "HWB记录错误",
               "user_message": "收到后续指令", "steer": "收到执行中追加指令"}


class DiscordError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Discord HTTP {status}，凭据与响应正文不记录")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_target(guild_id, channel_id):
    for value in (guild_id, channel_id):
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{16,19}", value) or int(value) >= 2**64:
            raise ValueError("guild/channel必须是17至20位的有效Discord ID")
    if guild_id == channel_id:
        raise ValueError("guild与父频道ID必须不同")
    return {"guild_id": guild_id, "channel_id": channel_id}


def validate_state_target(state, target):
    if "target" in state and state["target"] != target:
        raise ValueError("目标配置与已有state绑定不符，禁止改投其他频道")
    for key, row in state.get("messages", {}).items():
        if (key == "overview" or key.startswith("task:")) and row.get("id") and row.get("channel") != target["channel_id"]:
            raise ValueError("旧state父频道与配置不符，禁止改投其他频道")


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def alive(pid):
    if os.name != "posix":
        raise RuntimeError("此部署脚本仅支持POSIX主机")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # windows-footgun: ok -- POSIX guard above; deployment is macOS only.
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def classify(meta, pending, is_alive=alive):
    status = meta.get("status")
    if status in {"failed", "dead"}:
        return "失败", "HWB报告失败或进程退出"
    if status in {"running", "starting", "waiting_decision", "idle"} and meta.get("pid") and not is_alive(meta["pid"]):
        return "失败", "登记进程已退出；PID存活也不能证明任务有进展"
    if status in {"running", "starting", "waiting_decision"} and not meta.get("pid"):
        return "失败", "活动状态缺少登记进程"
    if pending or status == "waiting_decision":
        return "待决策", "待总控确认；看板不执行审批"
    if status in {"running", "starting"}:
        return "运行中", "执行中；业务进展见本地证据"
    if status in {"idle", "stopped"}:
        return "待总控核验", "已停止" if status == "stopped" else "执行者空闲；不等于完成"
    return "待决策", "未知HWB状态，需总控确认"


def safe_label(value, limit=100):
    # Publish identifiers, never prompts, tool input, model prose or decision contents.
    value = re.sub(r"[\r\n`<>@]", " ", str(value))
    value = re.sub(r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=[已隐藏]", value)
    return value[:limit]


def scan_tasks(home, *, task_id=None):
    tasks = []
    for path in sorted((home / "workers").glob("*/meta.json")):
        if task_id is not None and path.parent.name != task_id:
            continue
        meta = json.loads(path.read_text(encoding="utf-8-sig"))  # Broken snapshots fail closed.
        if meta.get("tool") not in {"claude", "codex"} or meta.get("id") != path.parent.name:
            continue
        directory = path.parent
        pending = [p.stem for p in (directory / "decisions").glob("[0-9]*.json")
                   if p.stem.isdigit() and not p.with_name(p.stem + ".answer.json").exists()]
        status, phase = classify(meta, pending)
        last = None
        events = directory / "events.jsonl"
        if events.exists():
            # A trailing, partially appended event is retried on the next poll.
            with events.open(encoding="utf-8-sig") as stream:
                for number, line in enumerate(stream, 1):
                    if not line.endswith("\n"):
                        break
                    event = json.loads(line)
                    if event.get("kind") in EVENT_NAMES:
                        last = (number, event)
        evidence = f"workers/{directory.name}/meta.json（status={safe_label(meta.get('status'))}）"
        progress = "HWB任务状态已登记；开发结果未独立核验"
        updated = meta.get("updated", "未核验")
        if last:
            number, event = last
            progress = EVENT_NAMES[event["kind"]]
            if event["kind"] == "decision" and not pending:
                progress = "历史待决策事件已落盘；当前无待答文件"
            if event["kind"] == "tool" and isinstance(event.get("exit_code"), int):
                progress += f"（退出码 {event['exit_code']}；不代表验收通过）"
            evidence += f"；events.jsonl:{number}（{event['kind']}）"
            updated = max(updated, event.get("t", ""))
        cwd_name = Path(meta.get("cwd", "")).name
        alias = safe_label((meta.get("name") or cwd_name).split("-")[0])
        tasks.append({"id": directory.name, "name": safe_label(meta.get("name") or directory.name),
                      "alias": alias, "executor": safe_label(meta["tool"]), "status": status,
                      "phase": phase, "progress": progress, "evidence": evidence,
                      "pending": pending, "updated": safe_label(updated), "acceptance": ACCEPTANCE,
                      "hwb_status": meta.get("status"), "worker_pid": meta.get("pid"),
                      "worker_alive": alive(meta.get("pid")),
                      "task_binding": digest({k: meta.get(k) for k in ("id", "created", "tool", "cwd")})})
    return tasks


def card(task):
    pending = "、".join("#" + n for n in task["pending"]) or "未发现待答决策文件；其他阻塞未核验"
    if task.get("disposition"):
        pending += "；总控说明：" + safe_label(task["disposition"], 240)
    return (f"【{task['alias']}】{task['name']}\n任务：{task['id']}\n"
            f"执行者：{task['executor']}（HWB）\n当前阶段：{task['status']}｜{task['phase']}\n"
            f"业务状态：{task['status']}\nHWB生命周期：{safe_label(task.get('hwb_status', '未核验'))}"
            f"（登记进程{'存活' if task.get('worker_alive') else '未发现存活'}；不代表业务验收）\n"
            f"最新可核验进展：{task['progress']}\n阻塞/待确认：{pending}（审批仍在总控）\n"
            f"三层验收：{task['acceptance']}\n证据：{task['evidence']}\n"
            f"总控记录：{(task.get('control_event') or {}).get('approval_ref', '无；worker自评不代替验收')}\n"
            f"源记录更新时间：{task['updated']}（Asia/Shanghai）")


def content_units(text):
    """Conservative platform budget, including non-BMP characters such as emoji."""
    return len(text.encode("utf-16-le")) // 2


def overview_entries(tasks, targets):
    groups = collections.defaultdict(list)
    for task in tasks:
        groups[(not task.get("closed"), task["alias"])].append(task)
    lines = []
    for (is_open, alias), rows in sorted(groups.items()):
        section = "待办分区" if is_open else "关闭后冲突"
        lines.append(f"【{section} · {safe_label(alias)}】{len(rows)}任务")
        for task in rows:
            target = targets.get(task["id"], {})
            entry = f"<#{target['thread_id']}>" if target.get("thread_id") else "入口未映射（需总控同步）"
            reason = task["phase"] if task.get("closed") else task.get("disposition")
            short = safe_label(reason, 24) if reason else ""
            detail = "；" + short + ("…" if len(str(reason)) > 24 else "") if short else ""
            lines.append(f"- {safe_label(task['name'])} {entry}｜{task['status']}{detail}")
    return "\n".join(lines)


def overview(tasks, targets=None, *, guild_id, channel_id=None):
    targets = targets or {}
    tasks = sorted((task for task in tasks if not (
        task.get("closed") and task["status"] in {"已完成", "已取消"})),
        key=lambda task: (not task.get("closed"), task["alias"], task["id"]))
    conflicts = sum(bool(task.get("closed")) for task in tasks)
    header = ("HWB 开发进度总览\n仅当前profile HWB；5分钟轮询。空闲不等于完成；关单仅采总控验收。\n"
              f"未关闭：{len(tasks) - conflicts}项；关闭后活动/pending冲突：{conflicts}项。\n")
    latest = max((task["updated"] for task in tasks), default="无任务")
    footer = f"\n最近源记录：{latest}（Asia/Shanghai）"
    body = overview_entries(tasks, targets) if tasks else "暂无未关闭任务。"
    complete = header + body + footer
    budget = 2000 - content_units(message_content("overview", ""))
    if content_units(complete) <= budget:
        return complete
    directory = f"<#{channel_id}>" if channel_id else "本频道"
    selected, result = [], ""
    for task in tasks:
        trial = selected + [task]
        omitted = len(tasks) - len(trial)
        notice = f"\n另有{omitted}项未展示；明细：{directory}的线程列表（含已归档），按任务名查看原卡。"
        candidate = header + overview_entries(trial, targets) + notice + footer
        if content_units(candidate) > budget:
            break
        selected, result = trial, candidate
    if not selected:
        result = header + f"另有{len(tasks)}项未展示；明细：{directory}的线程列表（含已归档）。" + footer
    return result


def stage_snapshot(task):
    result = {k: task[k] for k in ("status", "phase", "pending", "acceptance")}
    if task.get("control_head"):
        result["control_head"] = task["control_head"]
    return result


def message_content(key, text, receipt=None):
    suffix = ""
    if receipt is not None:
        suffix = "\nHWB阶段记录：" + json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    suffix += f"\n`[hwb-board:{digest(key)[:24]}]`"
    if len(suffix) > 1900:
        raise RuntimeError("阶段恢复信息过长，停止而非截断证据")
    if key == "overview":
        if content_units(text + suffix) > 2000:
            raise RuntimeError("总览超过平台上限，禁止截断任务条目")
        return text + suffix
    return text[:min(1800, 2000 - len(suffix))] + suffix


def validate_stage(stage):
    if not isinstance(stage, dict) or set(stage) not in (
            {"status", "phase", "pending", "acceptance"},
            {"status", "phase", "pending", "acceptance", "control_head"}):
        raise RuntimeError("阶段恢复信息格式未知")
    if stage["status"] not in {"运行中", "空闲待验收", "待验收", "待总控核验", "失败", "待决策", "已完成", "已取消"}:
        raise RuntimeError("阶段恢复信息状态未知")
    if not all(isinstance(stage[k], str) and stage[k] for k in ("phase", "acceptance")):
        raise RuntimeError("阶段恢复信息缺失")
    if not isinstance(stage["pending"], list) or any(not isinstance(n, str) for n in stage["pending"]):
        raise RuntimeError("阶段决策编号无效")
    if "control_head" in stage and (not isinstance(stage["control_head"], str) or not stage["control_head"]):
        raise RuntimeError("阶段总控记录无效")
    return stage


def legacy_stage(content):
    match = re.match(r"(?:首次接入|重要阶段变化)：([^\n]+)\n待决策编号：([^\n]+)（仅展示，审批在总控）\n([^\n]+)\n证据：", content)
    if not match or "｜" not in match[1]:
        raise RuntimeError("旧阶段内容无法可靠恢复，禁止复用序号")
    status, phase = match[1].split("｜", 1)
    stage = {"status": status, "phase": phase, "pending": [] if match[2] == "无" else match[2].split("、"),
             "acceptance": match[3]}
    head = re.search(r"^总控记录：([^\n]+)$", content, re.MULTILINE)
    if head and head[1] != "无，未验收":
        stage["control_head"] = head[1]
    return validate_stage(stage)


def recovered_stage(remote, tid, number, cached=None):
    content = remote["content"]
    receipts = re.findall(r"^HWB阶段记录：(.*)$", content, re.MULTILINE)
    if receipts:
        try:
            receipt, = receipts
            receipt = json.loads(receipt)
            if set(receipt) != {"task_id", "sequence", "stage"} or receipt["task_id"] != tid or type(receipt["sequence"]) is not int or receipt["sequence"] != number:
                raise ValueError("identity")
            return validate_stage(receipt["stage"])
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("阶段恢复信息损坏或身份冲突") from exc
    try:
        return legacy_stage(content)
    except RuntimeError:
        if cached is None:
            raise
        return validate_stage(cached)


class Discord:
    def __init__(self, token, guild_id, channel_id):
        validate_target(guild_id, channel_id)
        self.guild_id, self.channel_id = guild_id, channel_id
        self.token = token
        self.threads = set()
        self.operations = collections.Counter()
        self.thread_states = {}
        channel = self.request("GET", f"/channels/{self.channel_id}")
        if channel.get("id") != self.channel_id or channel.get("guild_id") != self.guild_id or channel.get("type") != 0:
            raise RuntimeError("目标频道/guild不符，停止同步")
        self.bot = self.request("GET", "/users/@me")["id"]

    def request(self, method, path, body=None):
        if path not in {"/users/@me", f"/guilds/{self.guild_id}/threads/active"}:
            match = re.fullmatch(r"/channels/(\d+)(/.*)?", path)
            if not match or match[1] not in {self.channel_id, *self.threads}:
                raise RuntimeError("拒绝访问授权频道以外的目标")
        elif method != "GET":
            raise RuntimeError("身份及用于恢复的thread目录仅可读取")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request("https://discord.com/api/v10" + path, data=data, method=method,
                                     headers={"Authorization": "Bot " + self.token,
                                              "Content-Type": "application/json",
                                              "User-Agent": "Hermes-HWB-Progress-Board/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    raw = response.read(4 * 1024 * 1024)
                    self.operations[method] += 1
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and method in {"GET", "DELETE"}:
                    return None
                if exc.code == 429:
                    retry = json.loads(exc.read()).get("retry_after", 2)
                    time.sleep(min(max(float(retry), 1), 30))
                elif exc.code >= 500 and method != "POST":
                    time.sleep(2 ** attempt)
                else:
                    raise DiscordError(exc.code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                # Ambiguous POSTs must recover from markers/anchored thread before resending.
                if method == "POST":
                    raise RuntimeError("Discord写入结果未知，下次按标记恢复") from None
                time.sleep(2 ** attempt)
        raise RuntimeError("Discord重试耗尽，下次cron继续")

    def history(self, channel):
        before = ""
        while True:
            rows = self.request("GET", f"/channels/{channel}/messages?limit=100" + before)
            if rows is None:
                raise RuntimeError("频道历史不可读，停止发送以防重复")
            yield from rows
            if len(rows) < 100:
                return
            before = "&before=" + rows[-1]["id"]

    def find_thread(self, names):
        active = self.request("GET", f"/guilds/{self.guild_id}/threads/active")["threads"]
        candidates = list(active)
        before = ""
        while True:
            page = self.request("GET", f"/channels/{self.channel_id}/threads/archived/public?limit=100" + before)
            candidates.extend(page["threads"])
            if not page.get("has_more"):
                break
            stamp = page["threads"][-1]["thread_metadata"]["archive_timestamp"]
            before = "&before=" + urllib.parse.quote(stamp, safe="")
        found = {t["id"]: t for t in candidates if t.get("name") in names
                 and t.get("parent_id") == self.channel_id and t.get("owner_id") == self.bot and t.get("type") == 11}
        if len(found) > 1:
            raise RuntimeError("同任务存在多个thread，停止以防分裂历史")
        return next(iter(found.values()), None)

    def thread(self, thread_id, name, legacy_name=None, *, create=True):
        if thread_id:
            self.threads.add(thread_id)
            try:
                thread = self.request("GET", f"/channels/{thread_id}")
            finally:
                self.threads.discard(thread_id)
            if thread is None:
                raise RuntimeError("已登记thread不存在，停止而非重建历史")
        else:
            thread = self.find_thread({name, legacy_name})
            if thread is None:
                if not create:
                    raise RuntimeError("关闭任务原thread不可恢复，禁止创建替代历史")
                thread = self.request("POST", f"/channels/{self.channel_id}/threads",
                                      {"name": name, "type": 11, "auto_archive_duration": 10080})
        if not thread or thread.get("parent_id") != self.channel_id or thread.get("guild_id") != self.guild_id or thread.get("type") != 11 or thread.get("owner_id") != self.bot:
            raise RuntimeError("thread归属不符")
        thread_id = thread["id"]
        self.threads.add(thread_id)
        self.thread_states[thread_id] = bool(thread.get("thread_metadata", {}).get("archived"))
        return thread_id

    def is_archived(self, thread_id, refresh=False):
        if refresh or thread_id not in self.thread_states:
            remote = self.request("GET", f"/channels/{thread_id}")
            if not remote or remote.get("parent_id") != self.channel_id or remote.get("guild_id") != self.guild_id:
                raise RuntimeError("归档状态读回失败")
            self.thread_states[thread_id] = bool(remote.get("thread_metadata", {}).get("archived"))
        return self.thread_states[thread_id]

    def set_archived(self, thread_id, archived):
        self.request("PATCH", f"/channels/{thread_id}", {"archived": archived})
        if self.is_archived(thread_id, refresh=True) != archived:
            raise RuntimeError("归档写入未通过读回")


class Board:
    def __init__(self, api, state_path):
        self.api = api
        self.path = state_path
        self.state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {"messages": {}, "tasks": {}}
        target = validate_target(api.guild_id, api.channel_id)
        validate_state_target(self.state, target)
        self.guild_id, self.channel_id = api.guild_id, api.channel_id
        self.state["target"] = target

    def save(self):
        atomic_json(self.path, self.state)

    def message(self, key, channel, text, receipt=None, *, embeds=None):
        from dev_progress_cards import payload_fingerprint, validate_payload
        marker = f"[hwb-board:{digest(key)[:24]}]"
        content = message_content(key, text, receipt)
        validate_payload(content, embeds or [])
        entry = self.state["messages"].setdefault(key, {})
        existing = None
        if entry.get("id"):
            existing = self.api.request("GET", f"/channels/{channel}/messages/{entry['id']}")
            if existing and (existing.get("author", {}).get("id") != self.api.bot or marker not in existing.get("content", "")):
                raise RuntimeError("拒绝编辑非本看板消息")
        if not existing:
            found = [m for m in self.api.history(channel)
                     if m.get("author", {}).get("id") == self.api.bot and marker in m.get("content", "")]
            if len(found) > 1:
                raise RuntimeError("发现重复标记，停止而非继续发送")
            existing = found[0] if found else None
        body = {"content": content, "allowed_mentions": {"parse": []}}
        if embeds is not None:
            body["embeds"] = embeds
        if not existing:
            # Stable nonce plus history reconciliation handles a crash before local state commit.
            body.update(nonce=digest(key)[:24], enforce_nonce=True)
            existing = self.api.request("POST", f"/channels/{channel}/messages", body)
        elif existing.get("content") != content or (embeds is not None and payload_fingerprint(existing) != payload_fingerprint(body)):
            if key.startswith(("stage:", "delivery:")):
                raise RuntimeError("阶段消息不可改写；恢复远端历史后使用新序号")
            existing = self.api.request("PATCH", f"/channels/{channel}/messages/{existing['id']}", body)
        if not existing or existing.get("channel_id") != channel:
            raise RuntimeError("消息写入/读回频道不符")
        entry.update(id=existing["id"], channel=channel, hash=digest(content))
        if embeds is not None:
            entry["payload_hash"] = payload_fingerprint(body)
        self.save()
        return existing["id"]

    def pin_overview(self, summary):
        remote = self.api.request("GET", f"/channels/{self.channel_id}/messages/{summary}")
        if not remote.get("pinned"):
            try:
                self.api.request("PUT", f"/channels/{self.channel_id}/messages/pins/{summary}")
                self.state.pop("pin_error", None)
            except DiscordError as exc:
                if exc.status != 403:
                    raise
                self.state["pin_error"] = "Discord403：bot缺少PIN_MESSAGES；需总控人工置顶或补该频道权限"
            self.save()
        else:
            self.state.pop("pin_error", None)

    def read_owned(self, key, channel, mid):
        remote = self.api.request("GET", f"/channels/{channel}/messages/{mid}")
        marker = f"[hwb-board:{digest(key)[:24]}]"
        if not remote or remote.get("channel_id") != channel or remote.get("author", {}).get("id") != self.api.bot or marker not in remote.get("content", ""):
            raise RuntimeError("消息读回/归属核验失败，禁止迁移删除")
        return remote

    def history_index(self, tid, thread):
        history = list(self.api.history(thread))
        keys = ["detail:" + tid] + [f"stage:{tid}:{n}" for n in range(1, len(history) + 1)]
        known = {digest(key)[:24]: key for key in keys}
        found = {}
        for remote in history:
            content = remote.get("content", "")
            markers = re.findall(r"\[hwb-board:([0-9a-f]{24})\]", content)
            if "[hwb-board:" not in content:
                continue
            if len(markers) != 1 or markers[0] not in known:
                raise RuntimeError("未知阶段标记，无法证明历史序号连续，停止写入")
            key = known[markers[0]]
            if remote.get("channel_id") != thread or remote.get("author", {}).get("id") != self.api.bot or not content.endswith(f"`[hwb-board:{markers[0]}]`"):
                raise RuntimeError("历史消息归属或标记冲突")
            if key in found:
                raise RuntimeError("历史阶段/详情卡标记重复，停止写入")
            cached = self.state["messages"].get(key, {})
            if cached.get("id") and (cached["id"] != remote["id"] or cached.get("channel") != thread):
                raise RuntimeError("历史消息映射冲突")
            if key.startswith("stage:") and cached.get("hash") and cached["hash"] != digest(content):
                raise RuntimeError("历史阶段内容与已记账哈希冲突")
            found[key] = remote
        prefix = "stage:" + tid + ":"
        if any(key.startswith(prefix) and key not in found for key in self.state["messages"]):
            raise RuntimeError("已记账阶段消息缺失，停止而非补写历史")
        return found

    def restore_task(self, task, entry, thread):
        tid, prefix = task["id"], "stage:" + task["id"] + ":"
        found = self.history_index(tid, thread)
        stages = {}
        for key, remote in found.items():
            if not key.startswith(prefix):
                continue
            number = int(key[len(prefix):])
            cached = self.state["messages"].get(key, {})
            fallback = entry.get("stage") if entry.get("sequence") == number and cached.get("hash") == digest(remote["content"]) else None
            stages[number] = recovered_stage(remote, tid, number, fallback)
        if sorted(stages) != list(range(1, len(stages) + 1)):
            raise RuntimeError("阶段历史不连续，停止而非猜测序号")
        saved_sequence = entry.get("sequence", 0)
        if type(saved_sequence) is not int or not 0 <= saved_sequence <= len(stages):
            raise RuntimeError("本地阶段序号与远端历史冲突，停止写入")
        detail = found.get("detail:" + tid)
        if not detail and (task.get("closed") or (stages and not entry.get("message_id"))):
            raise RuntimeError("原详情卡不可恢复，禁止创建替代历史")
        latest = stages.get(len(stages))
        self.restore_closed(task, entry, thread, detail, latest)
        for key, remote in found.items():
            self.state["messages"][key] = {"id": remote["id"], "channel": thread, "hash": digest(remote["content"])}
        entry.update(sequence=len(stages))
        if latest:
            entry["stage"] = latest
        if detail:
            entry.update(message_id=detail["id"], card_channel_id=thread)
        reopen = task.get("reopen_event")
        if reopen and any(s.get("control_head") == reopen["hash"] for s in stages.values()):
            entry["applied_reopen"] = reopen["hash"]
        self.save()

    def restore_closed(self, task, entry, thread, detail, latest):
        if not task.get("closed"):
            return
        head = task.get("control_head")
        if not latest or latest.get("control_head") != head:
            if self.api.is_archived(thread) or entry.get("applied_control_head") == head:
                raise RuntimeError("归档关闭阶段证据缺失或冲突，禁止解归档补写")
            return
        if latest["status"] not in {"已完成", "已取消"} or latest["acceptance"] != task["acceptance"]:
            raise RuntimeError("关闭阶段与独立验收记录冲突")
        if task["status"] in {"已完成", "已取消"} and latest != stage_snapshot(task):
            raise RuntimeError("关闭阶段快照与独立关单状态冲突")
        approval = (task.get("control_event") or {}).get("approval_ref")
        required = [f"任务：{task['id']}\n", f"业务状态：{latest['status']}\n",
                    f"三层验收：{latest['acceptance']}\n", f"总控记录：{approval}\n"]
        if not approval or not detail or any(line not in detail["content"] for line in required):
            raise RuntimeError("原关闭详情卡与关闭阶段证据冲突")
        from dev_progress_cards import validate_closed_embed
        validate_closed_embed(task, latest, detail)
        entry["applied_control_head"] = head
        if self.api.is_archived(thread):
            entry["archived_control_head"] = head
        entry.pop("sync_error", None)

    def sync_task(self, task):
        tid = task["id"]
        entry = self.state["tasks"].setdefault(tid, {"sequence": 0})
        legacy = self.state["messages"].get("task:" + tid, {})
        thread_id = entry.get("thread_id") or legacy.get("id")
        base = task["alias"] + "｜" + task["name"]
        name = base[:78] + " ·hwb-" + digest(tid)[:12]
        thread = self.api.thread(thread_id, name, base[:100], create=not task.get("closed"))
        entry["thread_id"] = thread
        self.restore_task(task, entry, thread)
        if entry.get("stage", {}).get("status") in {"空闲待验收", "待验收"} and task["status"] == "待总控核验" and not task.get("control_head"):
            entry["stage"]["status"] = "待总控核验"  # Rename legacy label without inventing a milestone.
        self.save()
        head = task.get("control_head")
        if task.get("closed"):
            if task["status"] not in {"已完成", "已取消"}:
                entry["sync_error"] = task["phase"]
                self.save()
                return
            if entry.get("applied_control_head") == head:
                return
        reopen = task.get("reopen_event")
        if not task.get("closed") and reopen and entry.get("applied_reopen") != reopen["hash"]:
            if self.api.is_archived(thread):
                self.api.set_archived(thread, False)
            entry["applied_reopen"] = reopen["hash"]
            self.save()
        if self.api.is_archived(thread):
            entry["sync_error"] = "线程已归档；仅新的总控reopen记录可解归档"
            task.update(status="待决策", phase=entry["sync_error"])
            self.save()
            return
        entry.pop("sync_error", None)
        if head and entry.get("message_id"):
            self.read_owned("detail:" + tid, thread, entry["message_id"])
        from dev_progress_cards import detail_content, hwb_detail
        mid = self.message("detail:" + tid, thread, detail_content(task), embeds=[hwb_detail(task, self.guild_id, thread)])
        self.read_owned("detail:" + tid, thread, mid)
        entry.update(message_id=mid, card_channel_id=thread)
        stage = stage_snapshot(task)
        if entry.get("stage") != stage:
            sequence = entry["sequence"] + 1
            self.message(f"stage:{tid}:{sequence}", thread,
                         f"{'首次接入' if not entry.get('stage') else '重要阶段变化'}：{task['status']}｜{task['phase']}\n"
                         f"待决策编号：{'、'.join(task['pending']) or '无'}（仅展示，审批在总控）\n"
                         f"{task['acceptance']}\n证据：{task['evidence']}\n"
                         f"总控记录：{head or '无，未验收'}\n"
                         f"操作原因：{(task.get('control_event') or {}).get('reason') or (task.get('control_event') or {}).get('payload', {}).get('reason') or 'HWB阶段变化，非验收'}\n"
                         f"记录时间：{task['updated']}（Asia/Shanghai）",
                         receipt={"task_id": tid, "sequence": sequence, "stage": stage})
            entry.update(stage=stage, sequence=sequence)
        if head:
            entry["applied_control_head"] = head
        self.save()

    def archive_closed(self, tasks):
        for task in tasks:
            if not task.get("closed") or task["status"] not in {"已完成", "已取消"}:
                continue
            entry = self.state["tasks"][task["id"]]
            if entry.get("applied_control_head") != task.get("control_head"):
                continue
            thread = entry["thread_id"]
            if not self.api.is_archived(thread, refresh=True):
                self.api.set_archived(thread, True)
            entry["archived_control_head"] = task["control_head"]
            self.save()

    def remove_legacy_cards(self):
        for key, old in list(self.state["messages"].items()):
            if not key.startswith("task:") or old.get("channel") != self.channel_id:
                continue
            tid = key.removeprefix("task:")
            entry = self.state["tasks"][tid]
            thread = entry["thread_id"]
            self.read_owned("detail:" + tid, thread, entry["message_id"])
            receipt = self.state.setdefault("migration", {}).setdefault(tid, {})
            if not receipt:
                receipt.update(old_message_id=old["id"], thread_id=thread,
                               history_before=[m["id"] for m in self.api.history(thread) if m.get("type") != 21])
                self.save()
            remote = self.api.request("GET", f"/channels/{self.channel_id}/messages/{old['id']}")
            if remote:
                self.read_owned(key, self.channel_id, old["id"])
                self.api.request("DELETE", f"/channels/{self.channel_id}/messages/{old['id']}")
            if self.api.request("GET", f"/channels/{self.channel_id}/messages/{old['id']}") is not None:
                raise RuntimeError("旧卡删除未通过读回")
            self.api.thread(thread, "", None)
            after = [m["id"] for m in self.api.history(thread) if m.get("type") != 21]
            if not set(receipt["history_before"]).issubset(after):
                raise RuntimeError("迁移后历史消息缺失，停止后续删除")
            receipt.update(history_after=after, deleted=True, card_id=entry["message_id"])
            del self.state["messages"][key]
            self.save()

    def sync(self, tasks, pool=None):
        tasks = [dict(task) for task in tasks]
        from dev_progress_delivery import validate, publish
        validate(self)
        from dev_progress_cards import restore_overview_pages
        restore_overview_pages(self)
        if pool is not None:
            pool.validate_anchor(self.state)
            from dev_progress_pool_display import validate_remote_pool
            validate_remote_pool(self, pool)
        for task in tasks:
            self.sync_task(task)
        targets = {tid: row for tid, row in self.state["tasks"].items() if row.get("card_channel_id")}
        prefix = ""
        if pool is not None:
            from dev_progress_pool_display import sync_pool
            prefix = sync_pool(self, pool, tasks, targets)
        from dev_progress_cards import sync_overview
        summary = sync_overview(self, tasks, targets, pool, prefix)
        self.pin_overview(summary)
        self.remove_legacy_cards()
        self.archive_closed(tasks)
        publish(self)
        self.state["layout_version"] = 2
        self.state["last_sync"] = datetime.now(TZ).isoformat()
        self.state["last_operations"] = dict(self.api.operations)
        self.state["coverage"] = "当前profile HWB及显式登记的问题/双来源任务池；仅展示轮询，无自主推进" if pool is not None else "仅当前profile HWB；仅5分钟轮询，无事件触发"
        self.save()


def token_from_home(home):
    # Read only this profile's existing secret file. Never copy credentials into deployment files.
    for line in (home / ".env").read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*(?:export\s+)?DISCORD_BOT_TOKEN\s*=\s*(.*)$", line)
        if match:
            values = shlex.split(match[1], comments=True)
            if len(values) == 1 and values[0]:
                return values[0]
    raise RuntimeError("当前profile未配置Discord bot认证")


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="Pinned owning Hermes profile home")
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--guild-id", required=True, help="Single authorized Discord guild ID")
    parser.add_argument("--channel-id", required=True, help="Single authorized parent text channel ID")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    target = validate_target(args.guild_id, args.channel_id)
    if args.home is None:
        # Hermes owns home resolution; deployment wrapper always supplies --home explicitly.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from hermes_constants import get_hermes_home
        args.home = get_hermes_home()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        from dev_progress_review import ReviewLog, auto_close, project_tasks, validate_delivery_anchor
        control = ReviewLog(args.state_dir / "control.json")
        state_path = args.state_dir / "state.json"
        prior_state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {"tasks": {}, "messages": {}}
        validate_state_target(prior_state, target)
        validate_delivery_anchor(control, prior_state)
        from dev_progress_pool import Pool
        pool = Pool(args.state_dir / "pool.json")
        pool.validate_anchor(prior_state)
        if not args.dry_run:
            auto_close(args.home, control, prior_state)
            atomic_json(state_path, prior_state)
        tasks = project_tasks(scan_tasks(args.home), control)
        if args.dry_run:
            preview = {"hwb_tasks": tasks, "pool": pool.data, "counts": pool.counts(tasks)} if pool.events else tasks
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            return
        api = Discord(token_from_home(args.home), args.guild_id, args.channel_id)
        board = Board(api, args.state_dir / "state.json")
        board.sync(tasks, pool=pool)
        # Cron delivers locally; empty stdout also prevents a second notification surface.


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print(f"HWB看板同步失败：{detail}；下次轮询重试", file=sys.stderr)
        raise SystemExit(1) from None
