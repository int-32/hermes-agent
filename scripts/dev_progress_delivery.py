"""Immutable publication inventories distinguish lost history from interrupted creation."""
import json
import re

from dev_progress_board import digest, message_content

PREFIX = "HWB发布清单："
KINDS = ("issues", "tasks", "windows", "index")


def publish_node(board, payload):
    key = "delivery:" + digest(payload)
    text = PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text) > 1700:
        raise RuntimeError("发布清单超过预算")
    mid = board.message(key, board.channel_id, text)
    return {"id": mid, "hash": digest(payload)}


def publish(board):
    entries = [{"key": key, "id": row["id"]} for key, row in sorted(board.state["messages"].items())
               if page_key(key) and row.get("id")]
    if not any(e["key"] != "overview" for e in entries):
        return
    children = [publish_node(board, {"entries": entries[n:n + 8]}) for n in range(0, len(entries), 8)]
    while len(children) > 8:
        children = [publish_node(board, {"children": children[n:n + 8]}) for n in range(0, len(children), 8)]
    root = publish_node(board, {"version": 1, "guild": board.guild_id, "channel": board.channel_id,
                                "count": len(entries), "children": children})
    board.state["delivery_root"] = root
    board.save()


def page_key(key):
    return key == "overview" or key == "pool:entry" or bool(re.fullmatch(r"(?:overview:page|pool:(?:issues|tasks|windows|index)):[1-9][0-9]*", key))


def validate(board):
    """Read all committed roots and referenced pages before the first remote mutation."""
    history = list(board.api.history(board.channel_id))
    by_id = {m["id"]: m for m in history}
    nodes, roots = {}, []
    for message in history:
        content = message.get("content", "")
        if not content.startswith(PREFIX):
            continue
        try:
            payload = json.loads(content[len(PREFIX):].split("\n", 1)[0])
        except (ValueError, TypeError) as exc:
            raise RuntimeError("发布清单格式损坏") from exc
        hashed = digest(payload)
        key = "delivery:" + hashed
        expected = message_content(key, PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if (message.get("author", {}).get("id") != board.api.bot or message.get("channel_id") != board.channel_id
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
        if root.get("version") != 1 or root.get("guild") != board.guild_id or root.get("channel") != board.channel_id:
            raise RuntimeError("发布清单目标冲突")
        entries = walk(nodes, root, set())
        if len(entries) != root.get("count") or len({e["key"] for e in entries}) != len(entries):
            raise RuntimeError("发布清单数量冲突")
        for entry in entries:
            key, mid = entry["key"], entry["id"]
            message = by_id.get(mid)
            marker = f"`[hwb-board:{digest(key)[:24]}]`"
            if (not page_key(key) or not message or message.get("author", {}).get("id") != board.api.bot
                    or not message.get("content", "").endswith(marker)):
                raise RuntimeError("已发布页面历史缺失或归属冲突，禁止重建")
            if key in recovered and recovered[key]["id"] != mid:
                raise RuntimeError("发布清单页面映射冲突")
            old = board.state["messages"].get(key, {})
            if old.get("id") and (old["id"] != mid or old.get("channel") != board.channel_id):
                raise RuntimeError("发布清单与本地映射冲突")
            recovered[key] = {"id": mid, "channel": board.channel_id, "hash": digest(message["content"])}
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
