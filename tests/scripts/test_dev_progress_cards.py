"""Native rich embeds retain delivery identity and controller recovery contracts."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import dev_progress_cards as cards
import dev_progress_review as review
from test_dev_progress_board import FakeDiscord, board, task
import test_dev_progress_pool as fixtures


def visible(message):
    return message.get("content", "") + json.dumps(message.get("embeds", []), ensure_ascii=False)


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.api = FakeDiscord()

    def pool_fixture(self):
        fixture = fixtures.PoolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_overview_and_hwb_detail_are_real_embeds_with_vertical_fields(self):
        item = dict(task("待决策"), pending=["7"], objective="目标尚未核验", acceptance_criteria="标准尚未核验")
        board.Board(self.api, self.state).sync([item])
        saved = json.loads(self.state.read_text())
        for key in ("overview", "detail:" + item["id"]):
            message = self.api.messages[saved["messages"][key]["id"]]
            self.assertTrue(message["embeds"])
            cards.validate_payload(message["content"], message["embeds"])
            self.assertNotIn("components", message)
            self.assertTrue(all(not f.get("inline") for e in message["embeds"] for f in e.get("fields", [])))
        detail = self.api.messages[saved["tasks"][item["id"]]["message_id"]]
        rendered = visible(detail)
        for required in ("目标", "验收", "进展", "下一步", "三层验收", "待决策", "codex"):
            self.assertIn(required, rendered)

    def test_legacy_text_migrates_in_place_and_second_sync_is_zero_writes(self):
        fixture = self.pool_fixture()
        fixture.goal()
        fixture.discover()
        fixture.window()
        board.Board(self.api, self.state).sync([task()], pool=fixture.log)
        saved = json.loads(self.state.read_text())
        detail = saved["tasks"][task()["id"]]["message_id"]
        overview = saved["messages"]["overview"]["id"]
        self.api.messages[detail].update(content=board.message_content("detail:" + task()["id"], board.card(task())), embeds=[])
        self.api.messages[overview].update(content=board.message_content("overview", "旧总览"), embeds=[])
        for key, row in saved["messages"].items():
            if key.startswith("pool:"):
                self.api.messages[row["id"]].update(content=board.message_content(key, "旧纯文本分页\n任务池记录：" + fixture.log.head), embeds=[])
        ids, threads = set(self.api.messages), dict(self.api.threads)
        self.state.unlink()
        board.Board(self.api, self.state).sync([task()], pool=fixture.log)
        self.assertEqual(set(self.api.messages), ids)
        self.assertEqual(self.api.threads, threads)
        self.assertTrue(self.api.messages[detail]["embeds"])
        self.assertTrue(self.api.messages[overview]["embeds"])
        for key, row in saved["messages"].items():
            if key.startswith("pool:"):
                self.assertTrue(self.api.messages[row["id"]]["embeds"])
        writes = self.api.operations.copy()
        # Discord adds read-only metadata and may omit inline:false on reads.
        for message in self.api.messages.values():
            for embed in message.get("embeds", []):
                embed["type"] = "rich"
                for field in embed.get("fields", []):
                    field.pop("inline", None)
        board.Board(self.api, self.state).sync([task()], pool=fixture.log)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(writes[method], self.api.operations[method])

    def test_embed_only_change_edits_and_normalized_payload_fingerprint_is_stable(self):
        controller = board.Board(self.api, self.state)
        embed = {"title": "中文卡片", "color": 123, "fields": [{"name": "状态", "value": "待核实", "inline": False}]}
        mid = controller.message("test-card", self.api.channel_id, "兼容标记", embeds=[embed])
        original_hash = controller.state["messages"]["test-card"]["payload_hash"]
        changed = copy.deepcopy(embed)
        changed["fields"][0]["value"] = "待分派"
        controller.message("test-card", self.api.channel_id, "兼容标记", embeds=[changed])
        self.assertEqual(self.api.operations["PATCH"], 1)
        self.assertNotEqual(controller.state["messages"]["test-card"]["payload_hash"], original_hash)
        remote = copy.deepcopy(self.api.messages[mid])
        remote["embeds"][0]["type"] = "rich"
        remote["embeds"][0]["fields"][0].pop("inline")
        self.assertEqual(cards.payload_fingerprint(remote), controller.state["messages"]["test-card"]["payload_hash"])

    def test_limits_include_all_embeds_fields_and_utf16_without_hiding_overflow(self):
        cards.validate_payload("字" * 2000, [{"title": "字" * 256, "description": "字" * 4096}])
        invalid = [
            [{"title": "字" * 257}], [{"description": "字" * 4097}],
            [{"fields": [{"name": "字段", "value": "字" * 1025}]}],
            [{"fields": [{"name": "字" * 257, "value": "值"}]}],
            [{"fields": [{"name": "项", "value": "值"}] * 26}],
            [{"description": "字" * 3100}] * 2,
            [{"title": "项"}] * 11, [{"footer": {"text": "字" * 2049}}],
            [{"author": {"name": "字" * 257}}], [{"title": "😀" * 129}],
        ]
        for embeds in invalid:
            with self.subTest(embeds=str(embeds)[:40]), self.assertRaises(ValueError):
                cards.validate_payload("", embeds)
        with self.assertRaises(ValueError):
            cards.validate_payload("字" * 2001, [])
        controller = board.Board(self.api, self.state)
        with self.assertRaises(ValueError):
            controller.message("bad", self.api.channel_id, "", embeds=invalid[0])
        self.assertFalse(self.api.calls)

    def test_priority_homepage_pool_entry_and_chinese_scale_have_no_missing_items(self):
        fixture = self.pool_fixture()
        for i in range(35):
            fixture.goal(f"目标-{i:03}")
            fixture.discover(key=f"检查-{i:03}", run=f"巡检-{i:03}")
        issue = next(iter(fixture.log.data["issues"].values()))
        # Register an actual high-severity observation through the same public protocol.
        fixture.apply("discover", {"source": "ops_inspection", "project": "项目", "fingerprint": "高严重项",
            "source_ref": "巡检-严重", "title": "高严重观察需先核实", "severity": "P0", "observed_at": "2026-10-24T00:00:00Z"}, "ops")
        fixture.window("目标-000")
        hwb = [dict(task("运行中"), id=f"执行-{i:03}", name="中文任务标题" + str(i)) for i in range(40)]
        hwb[-1].update(status="待决策", pending=["8"])
        board.Board(self.api, self.state).sync(hwb, pool=fixture.log)
        saved = json.loads(self.state.read_text())
        home = self.api.messages[saved["messages"]["overview"]["id"]]
        self.assertIn("高严重观察", visible(home))
        self.assertIn("待决策", visible(home))
        self.assertIn("问题 36｜任务 75｜执行窗口 41", visible(home))
        entry = self.api.messages[saved["messages"]["pool:entry"]["id"]]
        self.assertTrue(entry["embeds"])
        all_text = "\n".join(visible(m) for m in self.api.messages.values())
        for iid in fixture.log.data["issues"]:
            self.assertIn(iid, all_text)
        for tid in fixture.log.data["tasks"]:
            self.assertIn(tid, all_text)
        for message in self.api.messages.values():
            cards.validate_payload(message["content"], message.get("embeds", []))
        ids, writes = set(self.api.messages), self.api.operations.copy()
        board.Board(self.api, self.state).sync(hwb, pool=fixture.log)
        self.assertEqual(ids, set(self.api.messages))
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(writes[method], self.api.operations[method])
        self.assertEqual(issue["state"], "unverified")

    def test_archived_legacy_and_embed_recovery_remain_read_only_and_reject_visual_conflict(self):
        closed = dict(task("已完成"), closed=True, control_head="close-1",
            control_event={"hash": "close-1", "approval_ref": "approval:close", "action": "close"})
        board.Board(self.api, self.state).sync([task()])
        board.Board(self.api, self.state).sync([closed])
        saved = json.loads(self.state.read_text())
        mid = saved["tasks"][task()["id"]]["message_id"]
        writes = self.api.operations.copy()
        self.state.unlink()
        board.Board(self.api, self.state).sync([closed])
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(writes[method], self.api.operations[method])
        original = copy.deepcopy(self.api.messages[mid])
        field = next(f for f in self.api.messages[mid]["embeds"][0]["fields"] if f["name"] == "业务状态")
        field["value"] = "已取消"
        self.state.unlink()
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([closed])
        self.api.messages[mid] = original
        self.api.messages[mid].update(content=board.message_content("detail:" + task()["id"], board.card(closed)), embeds=[])
        board.Board(self.api, self.state).sync([closed])
        self.assertFalse(self.api.messages[mid]["embeds"])
        self.assertTrue(self.api.archived)

    def test_explicit_blocker_and_mapped_hwb_decisions_stay_above_unblocked_work(self):
        fixture = self.pool_fixture()
        fixture.goal()
        fixture.window()
        wid = next(iter(fixture.log.data["windows"]))
        fixture.apply("receipt", {"task_id": "t1", "window_id": wid, "kind": "accepted", "progress": "已接单",
            "next_step": "等待依赖", "blocker": "等待总控提供已授权文档"}, "ops")
        for i in range(8):
            fixture.goal(f"普通任务-{i}")
        board.Board(self.api, self.state).sync([], pool=fixture.log)
        saved = json.loads(self.state.read_text())
        home = self.api.messages[saved["messages"]["overview"]["id"]]
        self.assertIn("等待总控提供已授权文档", visible(home))
        self.assertEqual(home["embeds"][0]["color"], cards.COLORS["attention"])
        self.assertIn("https://discord.com/channels/100/200/300", visible(home))
        directory = fixture.home / "workers" / "worker-1"
        directory.mkdir(parents=True)
        meta = {"id": "worker-1", "tool": "codex", "status": "stopped", "pid": None,
                "created": "2026-10-24 00:00:00", "cwd": str(fixture.home)}
        (directory / "meta.json").write_text(json.dumps(meta))
        fixture.apply("window", {"task_id": "t1", "kind": "hwb", "profile": "owner", "session_id": "worker-1",
            "executor": "codex", "binding": review.binding(meta)})
        mapped = dict(task("待决策"), id="worker-1", pending=["5"], phase="等待总控答复#5")
        board.Board(self.api, self.state).sync([mapped], pool=fixture.log)
        home = self.api.messages[saved["messages"]["overview"]["id"]]
        self.assertIn("等待总控答复#5", visible(home))

    def test_color_only_edit_and_lost_embed_write_response_keep_same_message(self):
        controller = board.Board(self.api, self.state)
        self.api.lose_reply = True
        original = cards.make_card("需关注", [cards.field("状态", "待核实")])
        with self.assertRaises(RuntimeError):
            controller.message("lost-card", self.api.channel_id, "固定标记", embeds=[original])
        ids = set(self.api.messages)
        mid = board.Board(self.api, self.state).message("lost-card", self.api.channel_id, "固定标记", embeds=[original])
        self.assertEqual(ids, {mid})
        changed = copy.deepcopy(original)
        changed["color"] = cards.COLORS["attention"]
        board.Board(self.api, self.state).message("lost-card", self.api.channel_id, "固定标记", embeds=[changed])
        self.assertEqual(self.api.operations["PATCH"], 1)
        self.assertEqual(set(self.api.messages), ids)

    def test_overview_pagination_shrink_after_state_loss_does_not_leave_old_active_cards(self):
        tasks = [dict(task(), id=f"任务-{i}", name=f"应被替换的旧标题-{i}") for i in range(24)]
        board.Board(self.api, self.state).sync(tasks)
        saved = json.loads(self.state.read_text())
        old_pages = [row["id"] for key, row in saved["messages"].items() if key.startswith("overview:page:")]
        self.state.unlink()
        board.Board(self.api, self.state).sync([dict(tasks[0], name="仅保留一个活动项")])
        for mid in old_pages:
            self.assertNotIn("应被替换", visible(self.api.messages[mid]))
        writes = self.api.operations.copy()
        board.Board(self.api, self.state).sync([dict(tasks[0], name="仅保留一个活动项")])
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(writes[method], self.api.operations[method])

    def test_maximum_chinese_fields_and_long_window_links_paginate_inside_api_limits(self):
        fixture = self.pool_fixture()
        for i in range(16):
            fixture.apply("task", {"id": f"任务-{i}", "source": "approved_goal", "project": "中文项目" * 50,
                "title": "😀中文标题" * 60, "objective": "目标" * 250, "scope": "范围" * 250,
                "acceptance": "验收" * 250, "approval_ref": "批准" * 250, "issue_ids": []})
            fixture.apply("window", {"task_id": f"任务-{i}", "kind": "native", "profile": "原生运维",
                "session_id": f"会话-{i}", "executor": "运维" * 45,
                "url": "https://discord.com/channels/" + "1" * 180 + "/" + "2" * 180 + "/" + str(i + 1)})
        board.Board(self.api, self.state).sync([], pool=fixture.log)
        for message in self.api.messages.values():
            cards.validate_payload(message["content"], message.get("embeds", []))
        output = "\n".join(visible(m) for m in self.api.messages.values())
        for tid in fixture.log.data["tasks"]:
            self.assertIn(tid, output)
