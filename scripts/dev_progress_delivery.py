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
    found, unmarked = {}, set()
    for tid, meta in board.api.public_threads().items():
        board.api.thread(tid, "", create=False)  # Validates guild/parent/owner/type before history access.
        history = list(board.api.history(tid))
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
                    any("[hwb-board:" in m.get("content", "") for m in board.api.history(old["id"]))):
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
        if not content.startswith(PREFIX):
            continue
        if message.get("channel_id") != board.board_channels.get("recovery"):
            raise RuntimeError("发布清单必须位于已登记恢复窗口")
        try:
            payload = json.loads(content[len(PREFIX):].split("\n", 1)[0])
        except (ValueError, TypeError) as exc:
            raise RuntimeError("发布清单格式损坏") from exc
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
        for entry in entries:
            key, mid = entry["key"], entry["id"]
            channel = page_channel(board, key)
            message = by_id.get(mid)
            marker = f"`[hwb-board:{digest(key)[:24]}]`"
            if (not page_key(key) or not message or not channel or message.get("channel_id") != channel or entry.get("channel") != channel
                    or message.get("author", {}).get("id") != board.api.bot
                    or not message.get("content", "").endswith(marker)):
                raise RuntimeError("已发布页面历史缺失或归属冲突，禁止重建")
            if key in recovered and recovered[key]["id"] != mid:
                raise RuntimeError("发布清单页面映射冲突")
            old = board.state["messages"].get(key, {})
            if old.get("id") and (old["id"] != mid or old.get("channel") != channel):
                raise RuntimeError("发布清单与本地映射冲突")
            recovered[key] = {"id": mid, "channel": channel, "hash": digest(message["content"])}
    board.state["messages"].update(recovered)
    if not roots:
        validate_legacy(history)


def walk(nodes, node, ancestors):
    if "entries" in node:
        if set(node) != {"entries"} or not isinstance(node["entries"], list):
            raise RuntimeError("发布清单叶子无效")
        return node["entries"]
    result = []
    for ref in node.get("children", []):
        hashed = ref["hash"]
        if hashed in ancestors or hashed not in nodes or nodes[hashed][0] != ref["id"]:
            raise RuntimeError("发布清单分片缺失或循环")
        result.extend(walk(nodes, nodes[hashed][1], ancestors | {hashed}))
    return result


def validate_legacy(history):
    """Legacy n/N counters and the successful entry are evidence, never guessed IDs."""
    known_ids = {m["id"] for m in history}
    for message in history:
        if not message.get("content", "").endswith(f"`[hwb-board:{digest('overview')[:24]}]`"):
            continue
        for embed in message.get("embeds", []):
            for item in embed.get("fields", []):
                if item.get("name") == "全部任务":
                    links = re.findall(r"https://discord.com/channels/[0-9]+/[0-9]+/([0-9]+)", item.get("value", ""))
                    if not links or any(mid not in known_ids for mid in links):
                        raise RuntimeError("旧总览目录指向的历史分页缺失")
    contents = [m.get("content", "") for m in history]
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
