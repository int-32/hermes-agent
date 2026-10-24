"""Immutable publication inventories distinguish lost history from interrupted creation."""
import json
import re

from dev_progress_board import digest, message_content

PREFIX = "HWB发布清单："
KINDS = ("issues", "tasks", "windows", "index")
WINDOWS = {"discovery": "问题发现", "recovery": "看板恢复记录"}


def window_identity(board, role):
    key = "window:" + digest({"guild": board.guild_id, "parent": board.channel_id, "role": role})
    text = WINDOWS[role] + ("\n公共窗口：继承父频道可见性；问题核实、任务与验收见入口。" if role == "discovery" else "\n受控恢复记录：发布清单不可改写，请勿手工删除。")
    return key, text


def read_registration(board, role, tid, history):
    key, text = window_identity(board, role)
    marker = f"[hwb-board:{digest(key)[:24]}]"
    matches = [m for m in history if marker in m.get("content", "")]
    if not matches:
        return None
    if (len(matches) != 1 or matches[0].get("author", {}).get("id") != board.api.bot
            or matches[0].get("channel_id") != tid or matches[0].get("content") != message_content(key, text)):
        raise RuntimeError("看板窗口标记重复、损坏或归属冲突")
    message = matches[0]
    old = board.state["messages"].get(key, {})
    if old.get("id") and (old["id"] != message["id"] or old.get("channel") != tid):
        raise RuntimeError("看板窗口标记映射冲突")
    board.state["messages"][key] = {"id": message["id"], "channel": tid, "hash": digest(message["content"])}
    return {"id": tid, "marker_id": message["id"]}


def restore_windows(board):
    """A name only detects an unfinished creation; ownership plus marker proves identity."""
    cached = board.state.get("board_threads", {})
    board.board_channels = {}
    board.thread_histories = {}
    found, unmarked = {}, set()
    for tid, meta in board.api.public_threads().items():
        board.api.thread(tid, "", create=False)  # Validates guild/parent/owner/type before history access.
        history = list(board.api.history(tid))
        board.thread_histories[tid] = history
        marked = False
        for role in WINDOWS:
            row = read_registration(board, role, tid, history)
            if not row:
                continue
            if role in found:
                raise RuntimeError("同一窗口出现多个登记标记")
            marked = True
            found[role] = row
        if not marked and meta.get("name") in WINDOWS.values():
            unmarked.add(tid)
    for role, old in cached.items():
        if role not in WINDOWS:
            raise RuntimeError("未知窗口登记")
        if role in found:
            if old.get("id") != found[role]["id"] or (old.get("marker_id") and old["marker_id"] != found[role]["marker_id"]):
                raise RuntimeError("看板窗口身份映射冲突")
        elif old.get("pending") and not old.get("marker_id"):
            board.api.thread(old["id"], "", create=False)
            key, _ = window_identity(board, role)
            if (board.state["messages"].get(key, {}).get("id") or
                    any(re.search(r"\[hwb-board:[0-9a-f]{24}\]", m.get("content", "")) for m in board.api.history(old["id"]))):
                raise RuntimeError("未完成登记与已有历史冲突，不能补造窗口标记")
            found[role] = dict(old)
        else:
            raise RuntimeError("已登记看板窗口或标记丢失，禁止重建")
    if unmarked - {v["id"] for v in found.values()}:
        raise RuntimeError("同名窗口缺少登记标记；核对创建回执，禁止猜测或重复创建")
    if "discovery" in found and "recovery" not in found:
        raise RuntimeError("业务窗口已登记但恢复窗口丢失")
    if len({v["id"] for v in found.values()}) != len(found):
        raise RuntimeError("业务与恢复窗口不能共用线程")
    board.state["board_threads"] = found
    board.board_channels = {role: row["id"] for role, row in found.items()}


def ensure_window(board, role):
    if not hasattr(board, "board_channels"):
        restore_windows(board)
    # Register recovery first, so an established business window always has a locator.
    if role == "discovery":
        ensure_window(board, "recovery")
    row = board.state["board_threads"].get(role)
    if row and row.get("marker_id"):
        return row["id"]
    if not row:
        tid = board.api.thread(None, WINDOWS[role], recover=False)
        row = {"id": tid, "pending": True}
        board.state["board_threads"][role] = row
        board.board_channels[role] = tid
        board.save()  # Persist the acknowledged thread before its first message.
    key, text = window_identity(board, role)
    mid = board.message(key, row["id"], text)
    row.clear()
    row.update(id=board.board_channels[role], marker_id=mid)
    board.save()
    return row["id"]


def page_channel(board, key):
    return board.channel_id if key == "overview" else board.board_channels.get("discovery")


def histories(board):
    if not hasattr(board, "board_channels"):
        restore_windows(board)
    return [m for channel in [board.channel_id, *board.board_channels.values()] for m in board.api.history(channel)]


def prepare_write(board, channel):
    if channel in getattr(board, "board_channels", {}).values() and board.api.is_archived(channel):
        board.api.set_archived(channel, False)


def publish_node(board, payload):
    key = "delivery:" + digest(payload)
    text = PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text) > 1700:
        raise RuntimeError("发布清单超过预算")
    mid = board.message(key, ensure_window(board, "recovery"), text)
    return {"id": mid, "hash": digest(payload)}


def publish(board):
    entries = [{"key": key, "id": row["id"], "channel": row["channel"]} for key, row in sorted(board.state["messages"].items())
               if page_key(key) and row.get("id")]
    if not any(e["key"] != "overview" for e in entries):
        return
    children = [publish_node(board, {"entries": entries[n:n + 8]}) for n in range(0, len(entries), 8)]
    while len(children) > 8:
        children = [publish_node(board, {"children": children[n:n + 8]}) for n in range(0, len(children), 8)]
    root = publish_node(board, {"version": 1, "guild": board.guild_id, "channel": board.channel_id,
                                "discovery": board.board_channels["discovery"], "recovery": board.board_channels["recovery"],
                                "count": len(entries), "children": children})
    board.state["delivery_root"] = root
    board.save()


def page_key(key):
    return key == "overview" or key == "pool:entry" or bool(re.fullmatch(r"(?:overview:page|pool:(?:issues|tasks|windows|index)):[1-9][0-9]*", key))


def validate(board):
    """Read all committed roots and referenced pages before the first remote mutation."""
    history = histories(board)
    by_id = {m["id"]: m for m in history}
    nodes, roots = {}, []
    for message in history:
        content = message.get("content", "")
        if not content.startswith(PREFIX) or not re.search(r"\[hwb-board:[0-9a-f]{24}\]", content):
            continue
        if message.get("author", {}).get("id") != board.api.bot or message.get("channel_id") != board.board_channels.get("recovery"):
            raise RuntimeError("发布清单必须位于已登记恢复窗口")
        try:
            payload = json.loads(content[len(PREFIX):].split("\n", 1)[0])
        except (ValueError, TypeError) as exc:
            raise RuntimeError("发布清单格式损坏") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("发布清单必须为对象")
        hashed = digest(payload)
        key = "delivery:" + hashed
        expected = message_content(key, PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if (message.get("author", {}).get("id") != board.api.bot
                or content != expected or hashed in nodes):
            raise RuntimeError("发布清单归属、内容或唯一性冲突")
        nodes[hashed] = (message["id"], payload)
        if "version" in payload:
            roots.append(payload)
    cached = board.state.get("delivery_root")
    if cached and (cached["hash"] not in nodes or nodes[cached["hash"]][0] != cached["id"]):
        raise RuntimeError("已提交发布清单丢失")
    recovered = {}
    for root in roots:
        if (root.get("version") != 1 or root.get("guild") != board.guild_id or root.get("channel") != board.channel_id
                or root.get("discovery") != board.board_channels.get("discovery") or root.get("recovery") != board.board_channels.get("recovery")):
            raise RuntimeError("发布清单目标冲突")
        entries = walk(nodes, root, set())
        if len(entries) != root.get("count") or len({e["key"] for e in entries}) != len(entries):
            raise RuntimeError("发布清单数量冲突")
    # An orphan leaf is still immutable evidence of original IDs. Parents are
    # written after their children, so a dangling orphan branch is history loss too.
    for _, node in nodes.values():
        for entry in walk(nodes, node, set()):
            restore_entry(board, entry, by_id, recovered)
    board.state["messages"].update(recovered)
    controlled = controlled_pages(board, history)
    validate_navigation(board, controlled)
    if not nodes:
        validate_legacy(controlled)


def restore_entry(board, entry, by_id, recovered):
    if not isinstance(entry, dict) or set(entry) != {"key", "id", "channel"} or not all(isinstance(v, str) for v in entry.values()):
        raise RuntimeError("发布清单页面格式无效")
    key, mid = entry["key"], entry["id"]
    channel = page_channel(board, key)
    message = by_id.get(mid)
    marker = f"`[hwb-board:{digest(key)[:24]}]`"
    if (not page_key(key) or not message or not channel or message.get("channel_id") != channel or entry["channel"] != channel
            or message.get("author", {}).get("id") != board.api.bot or not message.get("content", "").endswith(marker)):
        raise RuntimeError("已发布页面历史缺失或归属冲突，禁止重建")
    if key in recovered and recovered[key]["id"] != mid:
        raise RuntimeError("发布清单页面映射冲突")
    old = board.state["messages"].get(key, {})
    if old.get("id") and (old["id"] != mid or old.get("channel") != channel):
        raise RuntimeError("发布清单与本地映射冲突")
    recovered[key] = {"id": mid, "channel": channel, "hash": digest(message["content"])}


def controlled_pages(board, history):
    count = max([len(history), board.state.get("overview_pages", 0), *board.state.get("pool_pages", {}).values()])
    keys = {"overview", "pool:entry", *board.state["messages"]}
    keys.update(f"pool:{kind}:{n}" for kind in KINDS for n in range(1, count + 1))
    keys.update(f"overview:page:{n}" for n in range(1, count + 1))
    markers = {digest(key)[:24]: key for key in keys if page_key(key)}
    found, seen = {}, set()
    for message in history:
        content = message.get("content", "")
        tokens = re.findall(r"\[hwb-board:([0-9a-f]{24})\]", content)
        matched = [markers[token] for token in tokens if token in markers]
        if not matched:
            continue
        key = matched[0]
        if (len(tokens) != 1 or key in seen or message.get("author", {}).get("id") != board.api.bot
                or message.get("channel_id") != page_channel(board, key) or not content.endswith(f"`[hwb-board:{tokens[0]}]`")):
            raise RuntimeError("受控导航页面标记、作者或频道冲突")
        found[message["id"]] = (key, message)
        seen.add(key)
    return found


def page_family(key):
    return key.rsplit(":", 1)[0] if key.rsplit(":", 1)[-1].isdigit() else key


# Renderer contract: these fields belong to the first Embed only. User titles,
# task fields and window descriptions cannot declare a page-navigation role.
NAVIGATION_FIELDS = {
    "overview": {"全部任务": ("overview:page",), "问题与任务入口": ("pool:entry",)},
    "pool:entry": {"分页目录": ("pool:index",)},
}
DIRECTORY_TARGETS = ("pool:issues", "pool:tasks", "pool:windows")


def navigation_sections(key, message):
    family = page_family(key)
    schema = dict(NAVIGATION_FIELDS.get(family, {}))
    if family in {"overview:page", *("pool:" + kind for kind in KINDS)} and int(key.rsplit(":", 1)[-1]) > 1:
        schema["分页导航"] = (key.rsplit(":", 1)[0] + ":" + str(int(key.rsplit(":", 1)[-1]) - 1),)
    reserved = {"全部任务", "问题与任务入口", "分页目录", "分页导航"}
    seen = set()
    for number, embed in enumerate(message.get("embeds", [])):
        for item in embed.get("fields", []):
            name = item.get("name")
            if name not in reserved:
                continue
            if number != 0 or name not in schema or name in seen:
                raise RuntimeError("系统导航字段与来源页面结构冲突")
            seen.add(name)
            yield schema[name], item.get("value", ""), True
        if number == 0 and family == "pool:index" and embed.get("description"):
            yield DIRECTORY_TARGETS, embed["description"], True
    required = set(schema) if family != "overview" and message.get("embeds") else set()
    if required - seen:
        raise RuntimeError("原生页面缺少固定系统导航字段")
    yield from legacy_navigation_sections(key, message.get("content", ""))


def legacy_navigation_sections(key, content):
    """Known fallback positions only; no user-visible link label is a protocol."""
    lines = content.splitlines()
    counter = r"问题 [0-9]+｜任务 [0-9]+｜执行窗口 [0-9]+"
    if key == "overview" and len(lines) > 2 and all(re.fullmatch(counter, line) for line in lines[:2]):
        yield ("pool:entry",), lines[2], True
    elif page_family(key) == "pool:index" or key == "pool:entry":
        # Old plain-text directories kept navigation after the fixed header/head.
        header = r"index [1-9][0-9]*/[0-9]+" if key != "pool:entry" else "问题发现入口"
        if not lines or not re.fullmatch(header, lines[0]):
            if "https://" in content or "](" in content:
                raise RuntimeError("未知目录页面格式，不能猜测原导航")
            return  # Text-only old cards still use the immutable page inventory.
        body = "\n".join(lines[2:-1])
        if "https://" in body or "](" in body:
            yield (*DIRECTORY_TARGETS, "pool:index"), body, True


def link_targets(text, required):
    links = list(re.finditer(r"\[[^\]\n]+\]\(([^\s)]+)\)", text))
    remainder = re.sub(r"\[[^\]\n]+\]\(([^\s)]+)\)", "", text)
    if (required and not links) or "https://" in remainder or "](" in remainder:
        raise RuntimeError("受控导航缺少原始页面链接或格式未知")
    for link in links:
        target = re.fullmatch(r"https://discord\.com/channels/([0-9]+)/([0-9]+)/([0-9]+)", link[1])
        if not target:
            raise RuntimeError("受控链接目标格式无效")
        yield target.groups()


def execution_targets(board):
    """Read original detail identities only inside already verified public threads."""
    found = {}
    for channel, history in board.thread_histories.items():
        if channel in board.board_channels.values():
            continue
        for message in history:
            content = message.get("content", "")
            tids = re.findall(r"^任务：([^\n]+)$", content, re.MULTILINE)
            if len(tids) != 1:
                continue
            key = "detail:" + tids[0]
            marker = f"`[hwb-board:{digest(key)[:24]}]`"
            if (not content.endswith(marker) or len(re.findall(r"\[hwb-board:[0-9a-f]{24}\]", content)) != 1
                    or message.get("author", {}).get("id") != board.api.bot or message.get("channel_id") != channel):
                continue
            found[message["id"]] = (key, message)
    return found


def validate_link(board, target, guild, channel, allowed):
    if (guild != board.guild_id or not target or target[1].get("channel_id") != channel
            or (target[0] not in allowed and page_family(target[0]) not in allowed)):
        raise RuntimeError("原导航指向的页面缺失或目标冲突，禁止替换历史")


def validate_navigation(board, controlled):
    executions = execution_targets(board)
    for key, message in controlled.values():
        for allowed, text, required in navigation_sections(key, message):
            for guild, channel, mid in link_targets(text, required):
                validate_link(board, controlled.get(mid), guild, channel, allowed)
        if page_family(key) == "pool:windows":
            for embed in message.get("embeds", []):
                for guild, channel, mid in link_targets(embed.get("description", ""), False):
                    target = executions.get(mid)
                    validate_link(board, target, guild, channel, (target[0],) if target else ())


def walk(nodes, node, ancestors):
    if "entries" in node:
        if set(node) != {"entries"} or not isinstance(node["entries"], list):
            raise RuntimeError("发布清单叶子无效")
        return node["entries"]
    allowed = {"children"} if "version" not in node else {"version", "guild", "channel", "discovery", "recovery", "count", "children"}
    if set(node) != allowed or not isinstance(node["children"], list) or not node["children"]:
        raise RuntimeError("发布清单分支无效")
    result = []
    for ref in node["children"]:
        if not isinstance(ref, dict) or set(ref) != {"id", "hash"} or not isinstance(ref["id"], str) or not isinstance(ref["hash"], str):
            raise RuntimeError("发布清单分片引用无效")
        hashed = ref["hash"]
        if hashed in ancestors or hashed not in nodes or nodes[hashed][0] != ref["id"]:
            raise RuntimeError("发布清单分片缺失或循环")
        result.extend(walk(nodes, nodes[hashed][1], ancestors | {hashed}))
    return result


def validate_legacy(controlled):
    """Legacy n/N counters and the successful entry are evidence, never guessed IDs."""
    contents = [m.get("content", "") for _, m in controlled.values()]
    published = any(c.startswith("问题发现入口\n任务池记录：") for c in contents)
    initial = not published and not any(c.startswith("问题 ") for c in contents) and any("\n发布协议：1" in c for c in contents)
    for kind in KINDS:
        pages = [re.match(rf"^{kind} ([1-9][0-9]*)/([0-9]+)\n", c) for c in contents]
        pages = [m for m in pages if m]
        numbers = {int(m[1]) for m in pages}
        expected = max([int(m[2]) for m in pages] + [max(numbers, default=0)])
        if (published and not pages) or (not initial and numbers != set(range(1, expected + 1))):
            raise RuntimeError("旧任务池分页清单不完整，须恢复原历史")
    overview = [re.match(r"^全部任务 · ([1-9][0-9]*)/([1-9][0-9]*)页", c) for c in contents]
    overview = [m for m in overview if m]
    if overview:
        expected = max(int(m[2]) for m in overview)
        # Retired pages are also history and are recovered by the original reader.
        numbers = {int(m[1]) for m in overview}
        if not initial and not set(range(1, expected + 1)).issubset(numbers):
            raise RuntimeError("旧总览分页清单不完整，须恢复原历史")
