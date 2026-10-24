"""Delivery projection only; pool.json remains the independent source of truth."""
import re
import copy

from dev_progress_board import digest, safe_label
from dev_progress_cards import active_counts, attention_fields, card_pages, field, issue_cards, make_card, normal_closed, pool_task_cards
from dev_progress_delivery import ensure_window, histories


def label(value, limit=140):
    clean = safe_label(value, limit + 1).replace("[", "（").replace("]", "）")
    return clean[:limit] + ("…（完整字段见snapshot）" if len(clean) > limit else "")






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


def sync_pages(board, kind, lines=(), *, cards=None):
    channel = ensure_window(board, "discovery")
    contents = (card_pages(cards) if cards is not None else
                [[make_card({"windows": "HWB窗口索引", "index": "问题与任务目录"}.get(kind, kind),
                            description=chunk)] for chunk in pages(lines)])
    ids = []
    previous = board.state.get("pool_pages", {}).get(kind, 0)
    for number in range(max(len(contents), previous)):
        key = f"pool:{kind}:{number + 1}"
        cached = board.state["messages"].get(key, {})
        if cached.get("id"):
            board.read_owned(key, channel, cached["id"])
        embeds = contents[number] if number < len(contents) else [make_card("本页记录已移动", [field("下一步", "请查看当前问题发现入口")])]
        # Work on a fresh rendering: pagination must not mutate a cached source card.
        embeds = copy.deepcopy(embeds)
        page_label = f"第{number + 1}/{len(contents)}页" if number < len(contents) else f"历史页{number + 1} · 当前共{len(contents)}页"
        embeds[0].setdefault("fields", []).append(field("分页", page_label))
        if ids:
            embeds[0]["fields"].append(field("分页导航", f"[上一页](https://discord.com/channels/{board.guild_id}/{channel}/{ids[-1]})"))
        ids.append(board.message(key, channel, f"{kind} {number + 1}/{len(contents)}\n任务池记录：{board.pool_head}\n发布协议：1", embeds=embeds))
    board.state.setdefault("pool_pages", {})[kind] = max(len(contents), previous)
    board.save()
    return ids[:len(contents)]


def sync_pool(board, pool, tasks, targets):
    channel = ensure_window(board, "discovery")
    pool.validate_anchor(board.state)
    board.pool_head = pool.head
    issues = sync_pages(board, "issues", cards=issue_cards(pool, targets, board.guild_id))
    task_ids = sync_pages(board, "tasks", cards=pool_task_cards(pool, targets, board.guild_id, tasks))
    window_lines = ["原HWB执行窗口索引（保留既有记录；不与关联池任务重复计数）"]
    for task in tasks:
        if normal_closed(task):
            continue
        target = targets.get(task["id"])
        if target:
            window_lines.append(f"[{label(task['name'])}](https://discord.com/channels/{board.guild_id}/{target['thread_id']}/{target['message_id']})｜{label(task['id'])}｜Owner：{label(task['executor'])}｜{label(task['status'])}")
    windows = sync_pages(board, "windows", window_lines)
    links = []
    for name, ids in (("问题", issues), ("任务", task_ids), ("HWB窗口", windows)):
        for number, mid in enumerate(ids, 1):
            links.append(f"[{name}{number}](https://discord.com/channels/{board.guild_id}/{channel}/{mid})")
    # Index pages also paginate, so the entry never silently drops a large pool.
    index = sync_pages(board, "index", links)
    cached = board.state["messages"].get("pool:entry", {})
    if cached.get("id"):
        board.read_owned("pool:entry", channel, cached["id"])
    counts = active_counts(tasks, pool)
    attention = attention_fields([], targets, pool, board.guild_id)
    entry_card = make_card("问题发现入口", [field("独立计数", f"问题 {counts['issues']}｜任务 {counts['tasks']}｜执行窗口 {counts['windows']}"),
        *attention[:2],
        field("运维巡检", "登记实际观察与证据；日志异常先待核实。无问题也可如实回传。"),
        field("批准目标", "总控先定义目标、范围、验收；接单后执行，回传后总控验收。"),
        field("分页目录", f"[问题 / 任务 / 执行窗口](https://discord.com/channels/{board.guild_id}/{channel}/{index[-1]})" + "\n多页沿上一页完整浏览")])
    if attention:
        entry_card["color"] = 0xD83C3E
    mid = board.message("pool:entry", channel, "问题发现入口\n任务池记录：" + pool.head, embeds=[entry_card])
    board.state["pool_head"] = pool.head
    board.save()
    return (f"问题 {counts['issues']}｜任务 {counts['tasks']}｜执行窗口 {counts['windows']}\n"
            f"[问题发现入口（公共窗口）](https://discord.com/channels/{board.guild_id}/{channel}/{mid})\n")


def validate_remote_pool(board, pool):
    """State loss must not turn a missing/rolled-back journal into an empty board."""
    known = {pool.head, *(event["hash"] for event in pool.events)}
    from dev_progress_review import ZERO
    known.add(ZERO)
    history = histories(board)
    channel = board.board_channels.get("discovery")
    count = max([len(history), *board.state.get("pool_pages", {}).values()])
    keys = ["pool:entry"] + [f"pool:{kind}:{n}" for kind in ("issues", "tasks", "windows", "index") for n in range(1, count + 1)]
    markers = {digest(key)[:24]: key for key in keys}
    found = {}
    for message in history:
        content = message.get("content", "")
        matches = re.findall(r"\[hwb-board:([0-9a-f]{24})\]", content)
        key = markers.get(matches[0]) if len(matches) == 1 else None
        if not key:
            if "任务池记录：" in content and matches:
                raise RuntimeError("未知任务池分页标记，停止恢复")
            continue
        if key in found or message.get("author", {}).get("id") != board.api.bot or message.get("channel_id") != channel or not content.endswith(f"`[hwb-board:{matches[0]}]`"):
            raise RuntimeError("任务池分页重复或归属/标记冲突，停止恢复")
        heads = re.findall(r"^任务池记录：([0-9a-f]{64})$", content, re.MULTILINE)
        if len(heads) != 1 or heads[0] not in known:
            raise ValueError("远端投递的任务池记录缺失或回退；先恢复pool.json备份")
        found[key] = message
    restore_pool_pages(board, found)


def restore_pool_pages(board, found):
    channel = board.board_channels.get("discovery")
    for key, cached in board.state["messages"].items():
        if key.startswith("pool:") and cached.get("id") and key not in found:
            raise RuntimeError("已记账任务池分页缺失，停止而非重建")
    for key, message in found.items():
        cached = board.state["messages"].get(key, {})
        if cached.get("id") and (cached["id"] != message["id"] or cached.get("channel") != channel):
            raise RuntimeError("任务池分页映射冲突，停止恢复")
        board.state["messages"][key] = {"id": message["id"], "channel": channel, "hash": digest(message["content"])}
        if key != "pool:entry":
            _, kind, number = key.split(":")
            counts = board.state.setdefault("pool_pages", {})
            counts[kind] = max(counts.get(kind, 0), int(number))
