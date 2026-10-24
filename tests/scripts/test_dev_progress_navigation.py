"""Navigation roles come from controlled page structure, never task display names."""
import copy
import json
import re

import pytest

from test_dev_progress_board import FakeDiscord, board, task
import test_dev_progress_pool as fixtures
import dev_progress_delivery as delivery


@pytest.fixture
def display():
    fixture = fixtures.PoolTests()
    fixture.setUp()
    try:
        yield fixture, FakeDiscord(), fixture.home / "state.json"
    finally:
        fixture.doCleanups()


def sync(display, rows):
    fixture, api, state = display
    board.Board(api, state).sync(rows, pool=fixture.log)
    return json.loads(state.read_text())


def assert_unchanged(api, before, messages, threads):
    assert api.messages == messages
    assert api.threads == threads
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        assert api.operations[method] == before[method]


@pytest.mark.parametrize("name", ["任务1", "问题1", "HWB窗口1", "目录1", "上一页"])
@pytest.mark.parametrize("phase", ["first", "unchanged", "recovery"])
def test_normal_window_names_keep_original_ids_and_zero_repeat_writes(display, name, phase):
    _, api, state = display
    rows = [dict(task(), name=name)]
    saved = sync(display, rows)
    target = saved["tasks"][rows[0]["id"]]
    window = api.messages[saved["messages"]["pool:windows:1"]["id"]]
    assert f"[{name}](https://discord.com/channels/{api.guild_id}/{target['thread_id']}/{target['message_id']})" in window["embeds"][0]["description"]
    if phase == "first":
        return
    before, messages, threads = api.operations.copy(), copy.deepcopy(api.messages), dict(api.threads)
    if phase == "recovery":
        state.unlink()
    restored = sync(display, rows)
    assert {k: v["id"] for k, v in restored["messages"].items()} == {k: v["id"] for k, v in saved["messages"].items()}
    assert restored["tasks"] == saved["tasks"]
    assert_unchanged(api, before, messages, threads)


def preflight(display):
    _, api, state = display
    controller = board.Board(api, state)
    delivery.restore_windows(controller)
    delivery.validate(controller)


@pytest.mark.parametrize("name", ["分页导航", "分页目录", "全部任务", "问题与任务入口", "[上一页](https://discord.com/channels/1/2/3)", "目录1\n分页导航"])
def test_user_names_cannot_create_system_fields_or_navigation(display, name):
    rows = [dict(task(), id=f"worker-{n}", name=name) for n in range(12)]
    saved = sync(display, rows)
    _, api, state = display
    before, messages, threads = api.operations.copy(), copy.deepcopy(api.messages), dict(api.threads)
    state.unlink()
    restored = sync(display, rows)
    assert restored["tasks"] == saved["tasks"]
    assert_unchanged(api, before, messages, threads)


@pytest.mark.parametrize("source,slot,target", [
    ("overview", "问题与任务入口", "pool:issues:1"),
    ("overview", "全部任务", "pool:entry"),
    ("pool:entry", "分页目录", "pool:windows:1"),
    ("pool:issues:2", "分页导航", "pool:tasks:1"),
    ("overview:page:2", "分页导航", "overview:page:2"),
    ("pool:windows:1", "分页目录", "pool:entry"),
])
def test_fixed_slots_reject_existing_controlled_targets_of_wrong_role(display, source, slot, target):
    fixture, api, state = display
    for n in range(8):
        fixture.discover(key=f"issue-{n}", run=f"run-{n}")
    saved = sync(display, [dict(task(), id=f"worker-{n}") for n in range(18)])
    message = api.messages[saved["messages"][source]["id"]]
    fields = message["embeds"][0].setdefault("fields", [])
    fields[:] = [f for f in fields if f["name"] != slot]
    row = saved["messages"][target]
    fields.append({"name": slot, "value": f"[常规修复](https://discord.com/channels/{api.guild_id}/{row['channel']}/{row['id']})"})
    state.unlink()
    before, messages, threads = api.operations.copy(), copy.deepcopy(api.messages), dict(api.threads)
    with pytest.raises(RuntimeError):
        preflight(display)
    assert_unchanged(api, before, messages, threads)


@pytest.mark.parametrize("damage", ["guild", "channel", "author", "missing", "marker", "page-as-window"])
def test_window_description_requires_original_hwb_detail_mapping(display, damage):
    saved = sync(display, [dict(task(), name="普通修复")])
    _, api, state = display
    target = saved["tasks"][task()["id"]]
    index = api.messages[saved["messages"]["pool:windows:1"]["id"]]["embeds"][0]
    if damage in {"guild", "channel"}:
        old = api.guild_id if damage == "guild" else target["thread_id"]
        index["description"] = index["description"].replace("/" + old + "/", "/999/")
    else:
        row = saved["messages"]["pool:issues:1"]
        mutations = {
            "page-as-window": lambda: index.update(description=f"[上一页](https://discord.com/channels/{api.guild_id}/{row['channel']}/{row['id']})"),
            "missing": lambda: api.messages.pop(target["message_id"]),
            "author": lambda: api.messages[target["message_id"]].update(author={"id": "human"}),
            "marker": lambda: api.messages[target["message_id"]].update(content=board.message_content("detail:foreign", "任务：" + task()["id"])),
        }
        mutations[damage]()
    state.unlink()
    before, messages, threads = api.operations.copy(), copy.deepcopy(api.messages), dict(api.threads)
    with pytest.raises(RuntimeError):
        preflight(display)
    assert_unchanged(api, before, messages, threads)


def test_index_description_role_survives_arbitrary_link_labels_and_missing_page(display):
    saved = sync(display, [])
    _, api, state = display
    for mid, message in list(api.messages.items()):
        if message["content"].startswith(delivery.PREFIX):
            del api.messages[mid]
    index = api.messages[saved["messages"]["pool:index:1"]["id"]]["embeds"][0]
    index["description"] = re.sub(r"\[[^\]]+\]", "[合法普通名称]", index["description"])
    del api.messages[saved["messages"]["pool:issues:1"]["id"]]
    del api.messages[saved["messages"]["pool:entry"]["id"]]
    api.messages[saved["messages"]["overview"]["id"]].update(content=board.message_content("overview", "旧总览"), embeds=[])
    state.unlink()
    before, messages, threads = api.operations.copy(), copy.deepcopy(api.messages), dict(api.threads)
    with pytest.raises(RuntimeError):
        preflight(display)
    assert_unchanged(api, before, messages, threads)


@pytest.mark.parametrize("source,slot", [("pool:entry", "分页目录"), ("pool:windows:2", "分页导航"), ("overview:page:2", "分页导航")])
@pytest.mark.parametrize("damage", ["deleted", "empty", "duplicate", "second-embed", "invalid-url"])
def test_native_navigation_structure_cannot_be_deleted_or_retyped(display, source, slot, damage):
    _, api, state = display
    saved = sync(display, [dict(task(), id=f"worker-{n}", name="中文标题" * 12) for n in range(20)])
    embed = api.messages[saved["messages"][source]["id"]]["embeds"][0]
    item = next(f for f in embed["fields"] if f["name"] == slot)
    mutations = {
        "deleted": lambda: embed["fields"].remove(item),
        "empty": lambda: item.update(value="无链接"),
        "duplicate": lambda: embed["fields"].append(dict(item)),
        "second-embed": lambda: (embed["fields"].remove(item), api.messages[saved["messages"][source]["id"]]["embeds"].append({"fields": [item]})),
        "invalid-url": lambda: item.update(value="[任务1](https://example.invalid/channels/1/2/3)"),
    }
    mutations[damage]()
    state.unlink()
    before, messages, threads = api.operations.copy(), copy.deepcopy(api.messages), dict(api.threads)
    with pytest.raises(RuntimeError):
        preflight(display)
    assert_unchanged(api, before, messages, threads)


def test_renderer_and_validator_navigation_contract_uses_keys_and_fixed_slots(display):
    fixture, api, _ = display
    for n in range(8):
        fixture.discover(key=f"contract-{n}", run=f"contract-{n}")
    saved = sync(display, [dict(task(), id=f"worker-{n}", name="上一页") for n in range(20)])
    mappings = {row["id"]: key for key, row in saved["messages"].items()}
    checked = set()
    for key, row in saved["messages"].items():
        if not delivery.page_key(key):
            continue
        message = api.messages[row["id"]]
        for embed in message.get("embeds", []):
            sections = [(f["name"], f["value"]) for f in embed.get("fields", [])]
            if key.startswith("pool:index:"):
                sections.append(("index-description", embed["description"]))
            for slot, value in sections:
                if slot not in {"分页目录", "问题与任务入口", "全部任务", "分页导航", "index-description"}:
                    continue
                targets = re.findall(r"https://discord.com/channels/([0-9]+)/([0-9]+)/([0-9]+)", value)
                assert targets
                for guild, channel, mid in targets:
                    assert guild == api.guild_id
                    assert channel == saved["board_threads"]["discovery"]["id"]
                    target_key = mappings[mid]
                    prefix, number = key.rsplit(":", 1) if ":" in key else (key, "")
                    previous = f"{prefix}:{int(number) - 1}" if number.isdigit() else ""
                    matches = {
                        "分页目录": key == "pool:entry" and target_key.startswith("pool:index:"),
                        "问题与任务入口": key == "overview" and target_key == "pool:entry",
                        "全部任务": key == "overview" and target_key.startswith("overview:page:"),
                        "分页导航": target_key == previous,
                        "index-description": target_key.rsplit(":", 1)[0] in {"pool:issues", "pool:tasks", "pool:windows"},
                    }
                    assert matches[slot]
                # Valid destinations remain valid under any visible label.
                if slot == "index-description":
                    embed["description"] = re.sub(r"\[[^\]]+\]", "[上一页]", value)
                else:
                    next(f for f in embed["fields"] if f["name"] == slot)["value"] = re.sub(r"\[[^\]]+\]", "[任务1]", value)
                checked.add(slot)
    assert checked == {"分页目录", "问题与任务入口", "全部任务", "分页导航", "index-description"}
    before, messages, threads = api.operations.copy(), copy.deepcopy(api.messages), dict(api.threads)
    preflight(display)
    assert_unchanged(api, before, messages, threads)
