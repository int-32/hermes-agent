"""Native Discord embeds, bounded for mobile and stable across API readbacks.

Markers and immutable milestone receipts stay in content, not embed footers.
"""
from __future__ import annotations

import re

from dev_progress_board import digest, safe_label

COLORS = {"attention": 0xD83C3E, "waiting": 0xD99000, "active": 0x2471A3,
          "closed": 0x64748B, "neutral": 0x5865F2}
STATUS = {"unverified": "待核实", "ready": "待分派", "executing": "执行中",
          "awaiting_review": "待验收", "closed": "关闭", "duplicate": "重复", "deferred": "暂不处理"}


def units(value):
    # UTF-16 is conservative for supplementary characters, including emoji.
    return len(value.encode("utf-16-le")) // 2


def clip(value, limit=180, *, multiline=False, stages=False):
    value = str(value)
    if multiline:
        limit = min(limit, units(clip(value, limit)))
        # U+2028 remains whitespace for safe_label's whole-text secret matcher,
        # while surviving its CR/LF flattening. Restore line breaks only afterward.
        value = safe_label(value.replace("\r", "\u2028").replace("\n", "\u2028"), max(len(value), limit)).replace("\u2028", "\n")
        if stages:
            value = re.sub(r"｜(?=(?:合入主线|上线验收)：)", "\n", value)
        # Break prose at existing boundaries, leaving URLs and their punctuation intact.
        value = "".join(part if part.startswith(("https://", "http://")) else
                        re.sub(r"[；;。](?=[^\n])", "\n", part).replace("｜", "\n")
                        for part in re.split(r"(https?://\S+)", value))
    else:
        value = safe_label(value, max(len(value), limit))
    value = value.replace("[", "（").replace("]", "）")
    if units(value) <= limit:
        return value
    hint = "…\n完整内容见材料引用/总控记录"
    suffix = hint if multiline and units(hint) <= limit else "…"
    result, count = [], 0
    for char in value:
        count += units(char)
        if count > limit - units(suffix):
            break
        result.append(char)
    prefix = "".join(result)
    if multiline and units(prefix) + units(suffix) < limit:
        suffix = "…" + suffix  # Keep the old packing weight at a surrogate-pair boundary.
    return prefix + suffix


def field(name, value):
    return {"name": name, "value": str(value) or "未提供", "inline": False}


def embed_units(embed):
    values = [embed.get("title", ""), embed.get("description", ""),
              embed.get("footer", {}).get("text", ""), embed.get("author", {}).get("name", "")]
    values.extend(value for item in embed.get("fields", []) for value in (item["name"], item["value"]))
    return sum(units(value) for value in values)


def validate_embed(embed):
    for value, maximum in ((embed.get("title", ""), 256), (embed.get("description", ""), 4096),
                           (embed.get("footer", {}).get("text", ""), 2048), (embed.get("author", {}).get("name", ""), 256)):
        if units(value) > maximum:
            raise ValueError("Embed文本超过Discord字段限额")
    if len(embed.get("fields", [])) > 25:
        raise ValueError("Embed超过25个字段")
    for item in embed.get("fields", []):
        if not item["name"] or not item["value"] or units(item["name"]) > 256 or units(item["value"]) > 1024:
            raise ValueError("Embed字段名/值无效或超限")


def validate_payload(content, embeds):
    if units(content) > 2000 or len(embeds) > 10:
        raise ValueError("Discord消息content或Embed数量超限")
    for embed in embeds:
        validate_embed(embed)
    if sum(embed_units(embed) for embed in embeds) > 6000:
        raise ValueError("Discord消息Embed合计超过6000字符")


def normalized_embeds(embeds):
    result = []
    for embed in embeds:
        row = {key: embed[key] for key in ("title", "description", "url", "color") if key in embed}
        if embed.get("fields"):
            row["fields"] = [{"name": f["name"], "value": f["value"], "inline": f.get("inline", False)} for f in embed["fields"]]
        for key in ("footer", "author"):
            if key in embed:
                row[key] = {name: value for name, value in embed[key].items() if name in {"text", "name", "url", "icon_url"}}
        result.append(row)
    return result


def payload_fingerprint(message):
    return digest({"content": message.get("content", ""), "embeds": normalized_embeds(message.get("embeds", []))})


def tone(status):
    if status in {"待决策", "失败"}:
        return "attention"
    if status in {"待核实", "待分派", "待验收", "待总控核验", "空闲待验收"}:
        return "waiting"
    return "closed" if status in {"已完成", "已取消", "关闭", "重复", "暂不处理"} else "active"


def make_card(title, fields=(), *, description="", status="待核实", footer=""):
    result = {"title": clip(title, 100), "color": COLORS[tone(status)], "fields": list(fields)}
    if description:
        result["description"] = description
    if footer:
        result["footer"] = {"text": footer}
    validate_payload("", [result])
    return result


def detail_content(task):
    approval = (task.get("control_event") or {}).get("approval_ref", "无；worker自评不代替验收")
    # Keep the original four closure predicates verbatim, in compact subtext.
    lines = [f"任务：{task['id']}", f"业务状态：{task['status']}",
             f"三层验收：{task['acceptance']}", f"总控记录：{approval}"]
    content = "-# 恢复校验记录（折叠）\n||\n" + "\n".join(lines) + "\n||\n"
    if units(content) > 1750:
        raise ValueError("关单恢复证据超长，不能截断后投递")
    return content


def hwb_detail(task, guild_id=None, thread_id=None):
    pending = "、".join("#" + value for value in task["pending"])
    next_step = pending + "｜待总控决策" if pending else task.get("phase", "待总控核验下一步")
    approval = (task.get("control_event") or {}).get("approval_ref", "无；尚未经总控验收")
    fields = [field("业务状态", task["status"]), field("执行者", clip(task["executor"], 80) + " · HWB"),
              field("目标 / 验收标准", clip(task.get("objective", "未独立登记目标；以总控任务书为准"), 220) + "\n" + clip(task.get("acceptance_criteria", "验收标准未独立登记；不从worker自评推断"), 220)),
              field("关键进展", clip(task["progress"], 400, multiline=True)), field("下一步 / 阻塞", clip(next_step, 400, multiline=True)),
              field("三层验收", clip(task["acceptance"], 900, multiline=True, stages=True)), field("总控记录", clip(approval, 400)),
              field("材料引用", clip(task["evidence"], 300)), field("源记录更新", clip(task["updated"], 60))]
    if thread_id and guild_id:
        fields.append(field("执行窗口", f"[HWB状态窗口](https://discord.com/channels/{guild_id}/{thread_id})\n原生会话链接未独立登记"))
    if task.get("disposition"):
        fields.append(field("总控未完成说明", clip(task["disposition"], 500, multiline=True)))
    return make_card(task["name"], fields, status=task["status"], footer="HWB任务：" + task["id"])


def validate_closed_embed(task, latest, detail):
    if not detail.get("embeds"):
        return  # Original pure-text certificates remain valid and read-only.
    embeds = detail["embeds"]
    expected = {"业务状态": (latest["status"],),
                "三层验收": (clip(latest["acceptance"], 900), clip(latest["acceptance"], 900, multiline=True, stages=True)),
                "总控记录": (clip(task["control_event"]["approval_ref"], 400),)}
    if len(embeds) != 1 or embeds[0].get("footer", {}).get("text") != "HWB任务：" + task["id"]:
        raise RuntimeError("关闭Embed任务身份冲突")
    fields = embeds[0].get("fields", [])
    for name, value in expected.items():
        actual = [f.get("value") for f in fields if f.get("name") == name]
        if len(actual) != 1 or actual[0] not in value:
            raise RuntimeError("关闭Embed展示与独立验收证据冲突")


def field_groups(fields, max_fields=12, limit=3800):
    result, current = [], []
    for item in fields:
        if current and (len(current) >= max_fields or sum(units(f["name"]) + units(f["value"]) for f in current + [item]) > limit):
            result.append(current)
            current = []
        current.append(item)
    result.append(current)
    return result


def split_card(title, fields, **kwargs):
    result = [make_card(title, group, **kwargs) for group in field_groups(fields)]
    for number, embed in enumerate(result, 1):
        embed["title"] += f" · {number}/{len(result)}"
    return result


def card_pages(embeds):
    result, current = [], []
    for embed in embeds:
        if current and (len(current) >= 3 or sum(embed_units(e) for e in current + [embed]) > 4800):
            result.append(current)
            current = []
        current.append(embed)
    if current:
        result.append(current)
    return result or [[make_card("暂无登记", [field("状态", "尚无记录；不合成问题或执行结果")])]]


def window_fields(pool, task, targets, guild_id):
    fields = []
    for wid in task["window_ids"]:
        window = pool.data["windows"][wid]
        receipt = task["receipts"].get(wid)
        value = clip(window["executor"], 80) + "\n" + ("已接单" if receipt else "拟分派，尚无接单证据")
        if window.get("url"):
            value += "\n[" + ("已核实执行窗口" if receipt else "登记链接 · 待核实") + "](" + window["url"] + ")"
        else:
            value += "\n原生会话链接尚未提供"
        target = targets.get(window["session_id"]) if window["kind"] == "hwb" else None
        if target:
            value += f"\n[HWB状态窗口](https://discord.com/channels/{guild_id}/{target['thread_id']}/{target['message_id']})"
        fields.append(field("执行者 / 窗口", value))
        pending = sum(c["window_id"] == wid and not c.get("confirmed_by") for c in task.get("claims", {}).values())
        if pending:
            fields.append(field("执行者声明 · 待核实", f"{pending}项未确认；不改变接单或当前进展，待总控原生读回。"))
        if receipt:
            fields.extend([field("关键进展", clip(receipt["progress"], 250, multiline=True)),
                           field("下一步 / 阻塞", clip(receipt["next_step"], 250, multiline=True)),
                           field("回传与材料", clip(receipt["at"], 50) + "\n" + clip(receipt["evidence"][0], 300))])
            if receipt.get("blocker"):
                fields.append(field("阻塞回传 · 待总控核实", clip(receipt["blocker"], 300, multiline=True)))
    return fields or [field("执行者 / 窗口", "尚未指定；无接单证据"), field("下一步 / 阻塞", "待总控明确分派")]


def pool_task_cards(pool, targets, guild_id, hwb_tasks):
    result = []
    hwb_rows = {row["id"]: row for row in hwb_tasks}
    for tid, task in sorted(pool.data["tasks"].items()):
        status = STATUS[pool.task_status(tid)]
        source = "运维巡检" if task["source"] == "ops_inspection" else "总控批准目标"
        fields = [field("业务状态", status + "\n" + task.get("outcome", "尚未关单")),
                  field("目标 / 范围", clip(task["objective"], 300) + "\n" + clip(task["scope"], 300)),
                  field("验收标准", clip(task["acceptance"], 500))]
        checked = task["review"]
        stale = bool(checked and not task["closed"] and not pool.review_valid(tid))
        checks = []
        for key, name in (("development", "开发完成"), ("merge", "合入主线"), ("release", "上线验收")):
            check = checked["checks"][key] if checked else {"status": "unverified"}
            label = "旧验收已失效 / 待重验" if stale else {"passed": "已核验", "unverified": "未核验", "not_applicable": "不适用"}[check["status"]]
            checks.append(name + "：" + label + ("\n" + clip(check["reason"], 150, multiline=True) if check.get("reason") else ""))
        fields.append(field("三层验收", "\n".join(checks)))
        fields.extend(window_fields(pool, task, targets, guild_id))
        for wid in task["window_ids"]:
            window = pool.data["windows"][wid]
            row = hwb_rows.get(window["session_id"]) if window["kind"] == "hwb" else None
            if task["closed"] and row and row["status"] not in {"已完成", "已取消"}:
                fields.append(field("待总控决策", "关闭后HWB状态冲突；不自动重开或停止执行者"))
            elif row and row["status"] in {"待决策", "失败"}:
                fields.append(field("HWB需关注", row["status"] + "\n" + clip(row["phase"], 300, multiline=True)))
        fields.append(field("登记与批准", source + "\n" + clip(task["project"], 60) + "\n" + clip(task["approval_ref"], 300)))
        fields.append(field("源记录更新", clip(task["updated"], 50)))
        for iid in task["issue_ids"]:
            fields.append(field("关联问题", iid))
        color_status = "待决策" if not task["closed"] and any(r.get("blocker") for r in task["receipts"].values()) else status
        # Replace only the middle-dot glyph; keep its spaces and original packing budget.
        result.extend(split_card(task["title"], fields, description="任务：" + tid + " \n " + status, status=color_status))
    return result


def issue_cards(pool, targets, guild_id):
    result = []
    items = sorted(pool.data["issues"].items(), key=lambda pair: (pair[1]["severity"], pair[0]))
    for iid, issue in items:
        observation = next((o for o in issue["observations"] if o["source_ref"] == issue.get("latest_observation")), issue["observations"][-1])
        status = STATUS[pool.issue_status(iid)]
        owners = sorted({pool.data["windows"][wid]["executor"] for tid in issue["task_ids"] for wid in pool.data["tasks"][tid]["window_ids"]})
        fields = [field("状态 / 严重级别", issue["severity"] + " \n " + status),
                  field("Owner", clip("、".join(owners) or "待总控分派", 200)),
                  field("处置 / 下一步", clip(issue["reason"], 300, multiline=True)),
                  field("来源与观察", clip(issue["project"], 80) + " \n 运维巡检\n" + clip(observation["observed_at"], 50) + f"\n{len(issue['observations'])}次观察"),
                  field("材料引用", clip(observation["evidence"][0], 300))]
        if issue.get("duplicate_of"):
            fields.append(field("重复原问题", issue["duplicate_of"]))
        if issue.get("escalation_pending"):
            fields.append(field("待总控决策", "严重观察新增或恶化；原处置未自动重开，须重新核实。"))
        for tid in issue["task_ids"]:
            fields.append(field("关联任务", tid))
            fields.extend(window_fields(pool, pool.data["tasks"][tid], targets, guild_id)[:1])
        color_status = "待决策" if issue["severity"] in {"P0", "P1"} and (issue.get("escalation_pending") or status not in {"关闭", "重复", "暂不处理"}) else status
        result.extend(split_card(issue["title"], fields, description="问题：" + iid + " \n " + issue["severity"] + " \n " + status, status=color_status))
    for run in pool.data["inspections"]:
        result.append(make_card("巡检回传 · " + clip(run["source_ref"], 45),
            [field("实际结果", clip(run["summary"], 500)), field("范围 / 时间", clip(run["project"], 100) + "\n" + clip(run["observed_at"], 50)),
             field("材料引用", clip(run["evidence"][0], 300))], description="不自动创建问题或确认缺陷"))
    return result


def summary_rows(tasks, targets, pool, guild_id):
    rows = []
    mapped = set()
    if pool is not None:
        for tid, task in sorted(pool.data["tasks"].items()):
            if task["closed"]:
                continue
            status = STATUS[pool.task_status(tid)]
            windows = [pool.data["windows"][wid] for wid in task["window_ids"]]
            mapped.update(w["session_id"] for w in windows if w["kind"] == "hwb")
            owner = "、".join(sorted({w["executor"] for w in windows})) or "待总控分派"
            links = ["[" + ("已核实执行窗口" if wid in task["receipts"] else "登记链接 · 待核实") + "](" + pool.data["windows"][wid]["url"] + ")"
                     for wid in task["window_ids"] if pool.data["windows"][wid].get("url")]
            blockers = [r["blocker"] for r in task["receipts"].values() if r.get("blocker")]
            priority = 2 if task["closed"] else 0 if blockers else 1
            value = "任务：" + tid + "\nOwner：" + clip(owner, 100) + "\n" + (links[0] if links else "原生窗口链接尚未提供")
            if blockers and not task["closed"]:
                value += "\n阻塞回传：" + clip(blockers[0], 180)
            rows.append((priority, field(clip(task["title"], 40) + " · " + status, value)))
    for task in tasks:
        if task["id"] in mapped or normal_closed(task):
            continue
        target = targets.get(task["id"])
        url = f"https://discord.com/channels/{guild_id}/{target['thread_id']}/{target['message_id']}" if target else None
        value = "任务：" + clip(task["id"], 100) + "\nOwner：" + clip(task["executor"], 80)
        value += "\n[HWB状态窗口](" + url + ")" if url else "\nHWB窗口尚未同步"
        if task.get("disposition"):
            value += "\n总控说明：" + clip(task["disposition"], 120)
        if task.get("closed"):
            value += "\n" + clip(task["phase"], 120)
        priority = 0 if task["status"] in {"待决策", "失败"} else 2 if task["status"] in {"已完成", "已取消"} else 1
        rows.append((priority, field(clip(task["name"], 40) + " · " + task["status"], value)))
    return sorted(rows, key=lambda row: row[0])


def normal_closed(task):
    return task.get("closed") and task["status"] in {"已完成", "已取消"}


def active_counts(tasks, pool=None):
    hwb = {task["id"] for task in tasks if not normal_closed(task)}
    if pool is None:
        return {"issues": 0, "tasks": len(hwb), "windows": len(hwb)}
    active = [task for task in pool.data["tasks"].values() if not task["closed"]]
    window_ids = {wid for task in active for wid in task["window_ids"]}
    mapped = {pool.data["windows"][wid]["session_id"] for wid in window_ids if pool.data["windows"][wid]["kind"] == "hwb"}
    issues = sum(i.get("escalation_pending", False) or pool.issue_status(iid) not in {"closed", "duplicate", "deferred"} for iid, i in pool.data["issues"].items())
    return {"issues": issues, "tasks": len(active) + len(hwb - mapped), "windows": len(window_ids) + len(hwb - mapped)}


def attention_fields(tasks, targets, pool, guild_id):
    fields = []
    # Also inspect mapped HWB windows: pool deduplication must not hide decisions.
    for task in sorted(tasks, key=lambda t: not t.get("closed")):
        if task["status"] not in {"待决策", "失败"}:
            continue
        target = targets.get(task["id"])
        value = clip(task["name"], 45) + "｜Owner：" + clip(task["executor"], 60) + "\n" + clip(task["phase"], 120)
        if target:
            value += f"\n[HWB状态窗口](https://discord.com/channels/{guild_id}/{target['thread_id']}/{target['message_id']})"
        fields.append(field("需关注 · " + task["status"], value))
    if pool is not None:
        urgent = [i for i in pool.data["issues"].values() if i["severity"] in {"P0", "P1"} and (i.get("escalation_pending") or pool.issue_status(i["id"]) not in {"closed", "deferred", "duplicate"})]
        for issue in sorted(urgent, key=lambda i: (i["severity"], i["id"])):
            windows = [pool.data["windows"][wid] for tid in issue["task_ids"] for wid in pool.data["tasks"][tid]["window_ids"]]
            owners = "、".join(sorted({w["executor"] for w in windows})) or "待总控核实/分派"
            value = clip(issue["title"], 80) + "\nOwner：" + clip(owners, 80) + "\n" + issue["id"]
            links = [w["url"] for w in windows if w.get("url")]
            if links:
                value += "\n[登记窗口 · 核实状态见任务详情](" + links[0] + ")"
            if issue.get("escalation_pending"):
                value += "\n严重级别恶化 · 待总控重新处置"
            fields.append(field("高严重 · " + issue["severity"] + " · " + STATUS[pool.issue_status(issue["id"])], value))
    return fields


def sync_overview(board, tasks, targets, pool, prefix):
    rows = summary_rows(tasks, targets, pool, board.guild_id)
    counts = active_counts(tasks, pool)
    stats = f"问题 {counts['issues']}｜任务 {counts['tasks']}｜执行窗口 {counts['windows']}"
    fields = [field("活动计数（正常关闭不计入）", stats.replace("｜", "\n"))]
    if not rows:
        fields.append(field("活动任务", "暂无未关闭任务。"))
    attention = attention_fields(tasks, targets, pool, board.guild_id)
    # Show at least one of each attention category before filling the mobile page.
    decisions = [f for f in attention if f["name"].startswith("需关注")]
    severe = [f for f in attention if f["name"].startswith("高严重")]
    fields.extend(decisions[:2] + severe[:2])
    if attention:
        fields.append(field("关注项计数", f"需关注窗口 {len(decisions)}\n高严重问题 {len(severe)}\n其余见完整目录"))
    shown = 0
    for _, item in rows[:5]:
        if sum(units(f["name"]) + units(f["value"]) for f in fields + [item]) > 4600:
            break
        fields.append(item)
        shown += 1
    navigation = sync_overview_pages(board, rows) if len(rows) > shown or board.state.get("overview_pages") else ""
    if navigation:
        fields.append(field("全部任务", navigation))
    if prefix:
        fields.append(field("问题与任务入口", prefix.split("\n", 1)[-1].strip()))
    status = "待决策" if attention or any(priority == 0 for priority, _ in rows) else "运行中"
    embed = make_card("问题与任务总览", fields, description="先处理待决策、阻塞及高严重观察；待核实不等于缺陷已确认。", status=status,
                      footer=f"首页展示 {shown}/{len(rows)} 任务 · 仅展示轮询，不自主推进")
    # A short fallback also preserves old consumers' direct task-window link.
    fallback = stats + "\n" + prefix + "\n" + ("\n".join(item["name"] + "\n" + item["value"] for _, item in rows[:1]) or "暂无未关闭任务。")
    return board.message("overview", board.channel_id, fallback, embeds=[embed])


def sync_overview_pages(board, rows):
    from dev_progress_delivery import ensure_window
    channel = ensure_window(board, "discovery")
    contents = field_groups([item for _, item in rows], max_fields=8, limit=4200)
    previous = board.state.get("overview_pages", 0)
    ids = []
    for number in range(max(previous, len(contents))):
        fields = list(contents[number]) if number < len(contents) else [field("记录已移动", "查看固定总览")]
        if ids:
            fields.append(field("分页导航", f"[上一页](https://discord.com/channels/{board.guild_id}/{channel}/{ids[-1]})"))
        title = f"全部任务 · {number + 1}/{len(contents)}页" if number < len(contents) else f"历史总览页{number + 1} · 记录已移动"
        embed = make_card(title, fields, footer=f"共 {len(rows)} 任务 · 当前{len(contents)}页 · 每页最多8项")
        ids.append(board.message(f"overview:page:{number + 1}", channel, title + "\n发布协议：1", embeds=[embed]))
    board.state["overview_pages"] = max(previous, len(contents))
    board.save()
    return f"[全部 {len(rows)} 任务 · {len(contents)}页](https://discord.com/channels/{board.guild_id}/{channel}/{ids[len(contents) - 1]})"


def restore_overview_pages(board):
    from dev_progress_delivery import histories
    history = histories(board)
    channel = board.board_channels.get("discovery")
    count = max(len(history), board.state.get("overview_pages", 0))
    keys = {digest(f"overview:page:{n}")[:24]: (f"overview:page:{n}", n) for n in range(1, count + 1)}
    found = {}
    for message in history:
        content = message.get("content", "")
        markers = re.findall(r"\[hwb-board:([0-9a-f]{24})\]", content)
        matched = [keys[marker] for marker in markers if marker in keys]
        if not matched:
            continue
        key, number = matched[0]
        if len(markers) != 1 or key in found or message.get("author", {}).get("id") != board.api.bot or message.get("channel_id") != channel:
            raise RuntimeError("总览分页重复或归属冲突，禁止写入")
        cached = board.state["messages"].get(key, {})
        if cached.get("id") and (cached["id"] != message["id"] or cached.get("channel") != channel):
            raise RuntimeError("总览分页映射冲突")
        found[key] = {"id": message["id"], "channel": channel, "hash": digest(content), "payload_hash": payload_fingerprint(message)}
        board.state["overview_pages"] = max(board.state.get("overview_pages", 0), number)
    required = {f"overview:page:{n}" for n in range(1, board.state.get("overview_pages", 0) + 1)}
    if required != set(found):
        raise RuntimeError("总览分页历史缺失，禁止重建")
    board.state["messages"].update(found)
