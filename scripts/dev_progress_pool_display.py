"""Delivery projection only; pool.json remains the independent source of truth."""
import re

from dev_progress_board import digest, safe_label
from dev_progress_pool import STATES


def label(value, limit=140):
    clean = safe_label(value, limit + 1).replace("[", "（").replace("]", "）")
    return clean[:limit] + ("…（完整字段见snapshot）" if len(clean) > limit else "")


def issue_lines(pool):
    lines = ["问题池｜日志异常不等于已确认缺陷；处置与关闭由总控核验。"]
    for iid, issue in sorted(pool.data["issues"].items()):
        observation = issue["observations"][-1]
        lines.extend([
            f"\n{label(iid)}｜{STATES[pool.issue_status(iid)]}｜{issue['severity']}｜{label(issue['project'])}",
            f"{label(issue['title'])}｜来源：运维巡检｜发现：{label(issue['observed_at'])}",
            f"最近观察：{label(observation['observed_at'])}｜登记更新：{label(issue['updated'])}｜观察次数：{len(issue['observations'])}",
            f"处置原因：{label(issue['reason'])}｜重复原问题：{label(issue.get('duplicate_of', '无'))}",
            "关联任务：" + ("无" if not issue["task_ids"] else ""),
        ])
        for tid in issue["task_ids"]:
            lines.append("关联任务：" + label(tid))
        for ref in observation["evidence"]:
            lines.append("来源证据：" + label(ref, 300))
    for run in pool.data["inspections"]:
        lines.append(f"巡检回传（不自动创建问题）：{label(run['source_ref'])}｜{label(run['project'])}｜{label(run['observed_at'])}｜{label(run['summary'])}")
        for ref in run["evidence"]:
            lines.append("巡检证据：" + label(ref, 300))
    return lines


def task_lines(pool, targets, guild_id, hwb_tasks):
    lines = ["双来源任务池｜登记/窗口关联不等于已接单；仅实际接单回执显示执行中。"]
    hwb_rows = {row["id"]: row for row in hwb_tasks}
    for tid, task in sorted(pool.data["tasks"].items()):
        source = "运维巡检（含运维自处理）" if task["source"] == "ops_inspection" else "总控批准目标"
        lines.extend([
            f"\n任务 {label(tid)}｜{STATES[pool.task_status(tid)]}｜{label(task.get('outcome', '尚未关单'))}",
            f"{label(task['title'])}｜项目：{label(task['project'])}｜来源：{source}",
            f"目标：{label(task['objective'])}｜范围：{label(task['scope'])}",
            f"验收标准：{label(task['acceptance'])}｜批准：{label(task['approval_ref'])}",
            f"更新：{label(task['updated'])}｜关联问题：" + ("无" if not task["issue_ids"] else ""),
        ])
        for iid in task["issue_ids"]:
            lines.append("关联问题：" + label(iid))
        checked = task["review"]
        for key, name in (("development", "开发完成"), ("merge", "合入主线"), ("release", "上线验收")):
            check = checked["checks"][key] if checked else {"status": "unverified"}
            lines.append(f"{name}：{check['status']}｜{label(check.get('reason', ''))}")
            for ref in check.get("evidence", []):
                lines.append("验收证据：" + label(ref, 300))
        if not task["window_ids"]:
            lines.append("执行者/执行窗口：尚未指定；尚无接单证据")
        for wid in task["window_ids"]:
            window = pool.data["windows"][wid]
            receipt = task["receipts"].get(wid)
            target = targets.get(window["session_id"]) if window["kind"] == "hwb" else None
            url = window.get("url")
            lines.append(f"执行者：{label(window['executor'])}｜profile：{label(window['profile'])}｜"
                         + ("已接单" if receipt else "拟分派；尚无接单证据"))
            lines.append(f"执行窗口：[{label(window['session_id'])}]({url})" if url else "原生会话链接：尚未提供；HWB执行标识为 " + label(window["session_id"]))
            if target:
                lines.append(f"[原HWB状态窗口](https://discord.com/channels/{guild_id}/{target['thread_id']}/{target['message_id']})")
            row = hwb_rows.get(window["session_id"]) if window["kind"] == "hwb" else None
            if task["closed"] and row and row["status"] not in {"已完成", "已取消"}:
                lines.append("关闭后HWB状态冲突：待总控决策；保留关闭记录，不自动重开或停止执行者")
            if receipt:
                lines.append(f"进展：{label(receipt['progress'])}｜下一步：{label(receipt['next_step'])}｜回传：{label(receipt['at'])}")
                for ref in receipt["evidence"]:
                    lines.append("接单/回传证据：" + label(ref, 300))
    return lines


def pages(lines, limit=1500):
    result, current = [], ""
    for line in lines:
        # Association lists can grow without an arbitrary per-task cardinality limit.
        while line:
            chunk, line = line[:limit], line[limit:]
            if len(current) + len(chunk) + 1 > limit:
                result.append(current)
                current = ""
            current += ("\n" if current else "") + chunk
    if current:
        result.append(current)
    return result


def sync_pages(board, kind, lines):
    contents = pages(lines)
    ids = []
    previous = board.state.get("pool_pages", {}).get(kind, 0)
    for number in range(max(len(contents), previous)):
        key = f"pool:{kind}:{number + 1}"
        cached = board.state["messages"].get(key, {})
        if cached.get("id"):
            board.read_owned(key, board.channel_id, cached["id"])
        body = contents[number] if number < len(contents) else "本页记录已移动，请查看当前问题发现入口。"
        if ids:
            body += f"\n[上一页](https://discord.com/channels/{board.guild_id}/{board.channel_id}/{ids[-1]})"
        ids.append(board.message(key, board.channel_id, f"{kind} {number + 1}/{len(contents)}\n{body}\n任务池记录：{board.pool_head}"))
    board.state.setdefault("pool_pages", {})[kind] = max(len(contents), previous)
    board.save()
    return ids[:len(contents)]


def sync_pool(board, pool, tasks, targets):
    pool.validate_anchor(board.state)
    board.pool_head = pool.head
    issues = sync_pages(board, "issues", issue_lines(pool))
    task_ids = sync_pages(board, "tasks", task_lines(pool, targets, board.guild_id, tasks))
    window_lines = ["原HWB执行窗口索引（保留既有记录；不与关联池任务重复计数）"]
    for task in tasks:
        target = targets.get(task["id"])
        if target:
            window_lines.append(f"[{label(task['name'])}](https://discord.com/channels/{board.guild_id}/{target['thread_id']}/{target['message_id']})｜{label(task['id'])}｜{label(task['status'])}")
    windows = sync_pages(board, "windows", window_lines)
    links = []
    for name, ids in (("问题", issues), ("任务", task_ids), ("HWB窗口", windows)):
        for number, mid in enumerate(ids, 1):
            links.append(f"[{name}{number}](https://discord.com/channels/{board.guild_id}/{board.channel_id}/{mid})")
    # Index pages also paginate, so the entry never silently drops a large pool.
    index = sync_pages(board, "index", links)
    entry = ("问题发现入口\n运维巡检：通过原生登记CLI提交观察及来源证据；日志异常先待核实。\n"
             "目标任务：总控先定义目标、范围与验收再登记。接单后执行，回传后由总控验收。\n"
             f"问题 {len(pool.data['issues'])}｜巡检回传 {len(pool.data['inspections'])}\n"
             f"[问题/任务目录](https://discord.com/channels/{board.guild_id}/{board.channel_id}/{index[-1]})（多页可沿上一页浏览）\n"
             f"任务池记录：{pool.head}")
    cached = board.state["messages"].get("pool:entry", {})
    if cached.get("id"):
        board.read_owned("pool:entry", board.channel_id, cached["id"])
    mid = board.message("pool:entry", board.channel_id, entry)
    counts = pool.counts(tasks)
    board.state["pool_head"] = pool.head
    board.save()
    return (f"问题 {counts['issues']}｜任务 {counts['tasks']}｜执行窗口 {counts['windows']}\n"
            f"[问题发现入口](https://discord.com/channels/{board.guild_id}/{board.channel_id}/{mid})\n")


def validate_remote_pool(board, pool):
    """State loss must not turn a missing/rolled-back journal into an empty board."""
    known = {pool.head, *(event["hash"] for event in pool.events)}
    from dev_progress_review import ZERO
    known.add(ZERO)
    history = list(board.api.history(board.channel_id))
    count = max([len(history), *board.state.get("pool_pages", {}).values()])
    keys = ["pool:entry"] + [f"pool:{kind}:{n}" for kind in ("issues", "tasks", "windows", "index") for n in range(1, count + 1)]
    markers = {digest(key)[:24]: key for key in keys}
    found = {}
    for message in history:
        content = message.get("content", "")
        matches = re.findall(r"\[hwb-board:([0-9a-f]{24})\]", content)
        key = markers.get(matches[0]) if len(matches) == 1 else None
        if not key:
            if "任务池记录：" in content and "[hwb-board:" in content:
                raise RuntimeError("未知任务池分页标记，停止恢复")
            continue
        if key in found or message.get("author", {}).get("id") != board.api.bot or message.get("channel_id") != board.channel_id or not content.endswith(f"`[hwb-board:{matches[0]}]`"):
            raise RuntimeError("任务池分页重复或归属/标记冲突，停止恢复")
        heads = re.findall(r"^任务池记录：([0-9a-f]{64})$", content, re.MULTILINE)
        if len(heads) != 1 or heads[0] not in known:
            raise ValueError("远端投递的任务池记录缺失或回退；先恢复pool.json备份")
        found[key] = message
    restore_pool_pages(board, found)


def restore_pool_pages(board, found):
    for key, cached in board.state["messages"].items():
        if key.startswith("pool:") and cached.get("id") and key not in found:
            raise RuntimeError("已记账任务池分页缺失，停止而非重建")
    for key, message in found.items():
        cached = board.state["messages"].get(key, {})
        if cached.get("id") and (cached["id"] != message["id"] or cached.get("channel") != board.channel_id):
            raise RuntimeError("任务池分页映射冲突，停止恢复")
        board.state["messages"][key] = {"id": message["id"], "channel": board.channel_id, "hash": digest(message["content"])}
        if key != "pool:entry":
            _, kind, number = key.split(":")
            counts = board.state.setdefault("pool_pages", {})
            counts[kind] = max(counts.get(kind, 0), int(number))
