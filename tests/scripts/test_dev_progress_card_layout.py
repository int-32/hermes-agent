"""Mobile Embed values are vertical without changing content or recovery identity."""
import copy
import json
import unittest

from test_dev_progress_board import FakeDiscord, board, task
import test_dev_progress_pool as fixtures
import dev_progress_cards as cards


def values(embed):
    return {item["name"]: item["value"] for item in embed["fields"]}


class CardLayoutTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PoolTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.api = FakeDiscord()
        self.state = self.f.home / "state.json"

    def sync(self, rows):
        board.Board(self.api, self.state).sync(rows, pool=self.f.log)
        return json.loads(self.state.read_text())

    def test_home_and_entry_counts_are_three_lines_with_original_content_fallback(self):
        saved = self.sync([task()])
        home = self.api.messages[saved["messages"]["overview"]["id"]]
        entry = self.api.messages[saved["messages"]["pool:entry"]["id"]]
        expected = ["问题 0", "任务 1", "执行窗口 1"]
        self.assertEqual(values(home["embeds"][0])["活动计数（正常关闭不计入）"].splitlines(), expected)
        self.assertEqual(values(entry["embeds"][0])["独立计数"].splitlines(), expected)
        self.assertEqual(home["content"].splitlines()[:2], ["问题 0｜任务 1｜执行窗口 1"] * 2)
        self.assertTrue(home["content"].endswith(f"`[hwb-board:{board.digest('overview')[:24]}]`"))

    def test_hwb_acceptance_and_controller_notes_are_vertical_without_changing_facts(self):
        row = dict(task(), disposition="开发：本地通过；合入：等待审查。上线：待总控验证", phase="等依赖；需总控确认。然后继续")
        saved = self.sync([row])
        detail = self.api.messages[saved["tasks"][row["id"]]["message_id"]]
        fields = values(detail["embeds"][0])
        self.assertEqual(fields["三层验收"].splitlines(), row["acceptance"].split("｜"))
        self.assertEqual(fields["总控未完成说明"].splitlines(), ["开发：本地通过；", "合入：等待审查。", "上线：待总控验证"])
        self.assertEqual(fields["下一步 / 阻塞"].splitlines(), ["等依赖；", "需总控确认。", "然后继续"])
        self.assertIn("三层验收：" + row["acceptance"], detail["content"])
        self.assertEqual(saved["tasks"][row["id"]]["stage"]["acceptance"], row["acceptance"])

    def test_issue_task_sources_owner_status_and_receipt_notes_keep_links_on_separate_lines(self):
        self.f.discover()
        iid = next(iter(self.f.log.data["issues"]))
        self.f.goal()
        self.f.apply("link", {"issue_id": iid, "task_id": "t1"})
        self.f.window()
        wid = next(iter(self.f.log.data["windows"]))
        claim = self.f.apply("receipt", {"task_id": "t1", "window_id": wid, "kind": "accepted",
            "progress": "完成检查；保留证据", "next_step": "等待确认；再继续", "blocker": "缺少材料；等待授权。不能自行操作"}, "ops")
        self.f.apply("confirm_receipt", {"task_id": "t1", "claim_id": claim["hash"], "approval_ref": "layout-readback"}, evidence=["sha256:" + "a" * 64])
        task_cards = cards.pool_task_cards(self.f.log, {}, self.api.guild_id, [])
        task_values = {k: v for embed in task_cards for k, v in values(embed).items()}
        self.assertEqual(task_values["业务状态"].splitlines(), ["执行中", "尚未关单"])
        self.assertEqual(task_values["登记与批准"].splitlines(), ["来源：总控批准目标", "项目：project", "批准：approval:goal"])
        self.assertEqual(task_values["执行者 / 窗口"].splitlines()[:2], ["Owner：ops", "状态：已接单"])
        url = self.f.log.data["windows"][wid]["url"]
        self.assertIn(f"[已核实执行窗口]({url})", task_values["执行者 / 窗口"].splitlines())
        self.assertEqual(task_values["阻塞回传 · 待总控核实"].splitlines(), ["缺少材料；", "等待授权。", "不能自行操作"])
        issue = cards.issue_cards(self.f.log, {}, self.api.guild_id)[0]
        issue_values = values(issue)
        self.assertEqual(issue_values["状态 / 严重级别"].splitlines(), ["严重级别：P2", "状态：待核实"])
        self.assertEqual(self.f.log.data["issues"][iid]["state"], "unverified")
        self.assertEqual(issue_values["Owner"], "ops")
        self.assertEqual(issue_values["来源与观察"].splitlines()[:2], ["来源：运维巡检", "项目：project"])

    def test_new_and_old_closed_acceptance_renderings_remain_read_only_and_exact(self):
        closed = dict(task("已完成"), closed=True, control_head="close-layout",
            control_event={"hash": "close-layout", "approval_ref": "approval:layout", "action": "close"})
        self.sync([task()])
        saved = self.sync([closed])
        mid = saved["tasks"][closed["id"]]["message_id"]
        detail = self.api.messages[mid]
        self.assertEqual(values(detail["embeds"][0])["三层验收"].splitlines(), closed["acceptance"].split("｜"))
        for legacy in (False, True):
            if legacy:
                next(f for f in detail["embeds"][0]["fields"] if f["name"] == "三层验收")["value"] = cards.clip(closed["acceptance"], 900)
            before, original = self.api.operations.copy(), copy.deepcopy(self.api.messages)
            self.state.unlink()
            self.sync([closed])
            self.assertEqual(self.api.messages, original)
            for method in ("POST", "PATCH", "PUT", "DELETE"):
                self.assertEqual(self.api.operations[method], before[method])
        next(f for f in detail["embeds"][0]["fields"] if f["name"] == "三层验收")["value"] = "开发完成：已核验\n合入主线：未核验\n上线验收：未核验"
        self.state.unlink()
        with self.assertRaises(RuntimeError):
            self.sync([closed])

    def test_long_multiline_chinese_fields_stay_within_discord_budget_and_keep_full_source(self):
        note = "开发：等待总控确认；合入：保留原始证据。上线：未核验😀\n" * 80
        row = dict(task(), disposition=note, progress=note)
        source = copy.deepcopy(row)
        saved = self.sync([row])
        fields = values(self.api.messages[saved["tasks"][row["id"]]["message_id"]]["embeds"][0])
        self.assertIn("\n", fields["总控未完成说明"])
        self.assertIn("完整", fields["总控未完成说明"])
        self.assertLessEqual(cards.units(fields["总控未完成说明"]), 500)
        self.assertEqual(row, source)
        for message in self.api.messages.values():
            cards.validate_payload(message["content"], message.get("embeds", []))
        before, original = self.api.operations.copy(), copy.deepcopy(self.api.messages)
        self.state.unlink()
        self.sync([row])
        self.assertEqual(self.api.messages, original)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(self.api.operations[method], before[method])

    def test_prose_breaks_preserve_explicit_lines_urls_and_utf16_budget_without_fixed_width_wrapping(self):
        url = "https://example.invalid/evidence?a=1;b=2"
        text = "开发：待核验；合入：待总控。\n上线：待验证\n" + url
        self.assertEqual(cards.clip(text, 500, multiline=True), "开发：待核验；\n合入：待总控。\n上线：待验证\n" + url)
        self.assertEqual(cards.clip("长句" * 80, 500, multiline=True), "长句" * 80)
        for limit in (40, 150, 300, 500, 900, 1024):
            clipped = cards.clip("😀开发完成；合入主线。上线待验收\n" * 200, limit, multiline=True)
            self.assertLessEqual(cards.units(clipped), limit)
            self.assertIn("\n", clipped)
            self.assertIn("完整内容见材料引用/总控记录", clipped)

    def test_existing_cards_only_patch_embeds_without_new_ids_content_or_stages(self):
        rows = [dict(task(), disposition="等合入；待上线")]
        saved = self.sync(rows)
        home = self.api.messages[saved["messages"]["overview"]["id"]]
        entry = self.api.messages[saved["messages"]["pool:entry"]["id"]]
        detail = self.api.messages[saved["tasks"][rows[0]["id"]]["message_id"]]
        replacements = [(home, "活动计数（正常关闭不计入）", "问题 0｜任务 1｜执行窗口 1"),
                        (entry, "独立计数", "问题 0｜任务 1｜执行窗口 1"),
                        (detail, "三层验收", rows[0]["acceptance"]),
                        (detail, "总控未完成说明", rows[0]["disposition"])]
        for message, name, value in replacements:
            next(f for f in message["embeds"][0]["fields"] if f["name"] == name)["value"] = value
        before, contents = self.api.operations.copy(), {mid: m["content"] for mid, m in self.api.messages.items()}
        updated = self.sync(rows)
        self.assertGreater(self.api.operations["PATCH"], before["PATCH"])
        self.assertEqual({mid: m["content"] for mid, m in self.api.messages.items()}, contents)
        self.assertEqual(updated["tasks"], saved["tasks"])
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.api.operations[method], before[method])
