"""HWB board state contracts: no completion inference, crash recovery and silent polls."""
import collections
import copy
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "dev_progress_board", Path(__file__).resolve().parents[2] / "scripts/dev_progress_board.py")
board = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(Path(SPEC.origin).parent))
SPEC.loader.exec_module(board)
importlib.import_module("dev_progress_cards")
importlib.import_module("dev_progress_pool")
importlib.import_module("dev_progress_pool_display")
importlib.import_module("dev_progress_delivery")
sys.path.pop(0)
TEST_GUILD = "100000000000000001"
TEST_CHANNEL = "100000000000000002"


class FakeDiscord:
    def __init__(self, guild_id=TEST_GUILD, channel_id=TEST_CHANNEL):
        self.guild_id, self.channel_id = guild_id, channel_id
        self.bot = "bot"
        self.operations = collections.Counter()
        self.messages = {}
        self.threads = {}
        self.next_id = 100
        self.lose_reply = False
        self.calls = []
        self.lose_delete_reply = False
        self.archived = set()
        self.archive_ack_lost = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        self.operations[method] += 1
        parts = path.split("/")
        channel = parts[2]
        if method == "POST":
            self.next_id += 1
            message = {"id": str(self.next_id), "channel_id": channel,
                       "author": {"id": self.bot}, "content": body["content"], "pinned": False}
            if "embeds" in body:
                message["embeds"] = copy.deepcopy(body["embeds"])
            self.messages[message["id"]] = message
            if self.lose_reply:
                self.lose_reply = False
                raise RuntimeError("response lost after server commit")
            return dict(message)
        mid = parts[-1]
        if method == "PUT":
            self.messages[mid]["pinned"] = True
            return {}
        message = self.messages.get(mid)
        if message is None:
            return None
        if method == "DELETE":
            del self.messages[mid]
            if self.lose_delete_reply:
                self.lose_delete_reply = False
                raise RuntimeError("delete reply lost after commit")
            return {}
        if method == "PATCH":
            message.update(body)
        return dict(message)

    def history(self, channel):
        return [dict(m) for m in self.messages.values() if m["channel_id"] == channel]

    def thread(self, thread_id, name, legacy_name=None, *, create=True):
        if thread_id:
            if thread_id not in self.threads:
                raise RuntimeError("registered thread missing")
            return thread_id
        found = [tid for tid, value in self.threads.items() if value in {name, legacy_name}]
        if len(found) > 1:
            raise RuntimeError("duplicate threads")
        if found:
            return found[0]
        if not create:
            raise RuntimeError("closed thread missing")
        self.next_id += 1
        thread_id = str(self.next_id)
        self.threads[thread_id] = name
        return thread_id

    def is_archived(self, thread_id, refresh=False):
        return thread_id in self.archived

    def set_archived(self, thread_id, archived):
        self.calls.append(("PATCH", f"/channels/{thread_id}", {"archived": archived}))
        self.operations["PATCH"] += 1
        self.archived.add(thread_id) if archived else self.archived.discard(thread_id)
        if archived and self.archive_ack_lost:
            self.archive_ack_lost = False
            raise RuntimeError("archive ack lost after commit")


def task(status="运行中"):
    return {"id": "任务-1", "alias": "项目", "name": "开发任务", "executor": "codex",
            "status": status, "phase": status, "progress": "工具调用记录已落盘",
            "pending": [], "evidence": "workers/任务-1/events.jsonl:2",
            "updated": "2026-10-22 01:00:00", "acceptance": board.ACCEPTANCE}


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = FakeDiscord()
        self.state = self.root / "state.json"

    def closed_task(self):
        return dict(task("已完成"), closed=True, control_head="close-1",
                    control_event={"hash": "close-1", "at": "2026-10-22 02:00:00",
                                   "action": "close", "reason": "总控明确核验", "approval_ref": "discord:approved"})

    def overview_tasks(self, count):
        aliases = ("discord", "小贷项目", "总控", "版权项目", "自动运维")
        rows = [dict(task("待总控核验"), id=f"open-{n:03d}", alias=aliases[n % len(aliases)],
                     name=f"{aliases[n % len(aliases)]}-交付与部署核验任务第{n:03d}项",
                     disposition="尚缺总控核验回执；需独立复核测试、主线提交及环境验收。" * 30)
                for n in range(count)]
        targets = {row["id"]: {"thread_id": str(1000000000000000100 + n),
                               "message_id": str(1000000000000001000 + n)} for n, row in enumerate(rows)}
        return rows, targets

    def test_overview_local_task_scale_keeps_every_name_entry_and_short_reason(self):
        for count in (15, 17):
            with self.subTest(count=count):
                rows, targets = self.overview_tasks(count)
                summary = board.overview(rows, targets, guild_id=TEST_GUILD)
                content = board.message_content("overview", summary)
                delivered = json.loads(json.dumps({"content": content}, ensure_ascii=False))["content"]
                self.assertLessEqual(len(delivered.encode("utf-16-le")) // 2, 2000)
                self.assertEqual(delivered, summary + board.message_content("overview", ""))
                entries = [line for line in delivered.splitlines() if line.startswith("- ")]
                self.assertEqual(len(entries), len(rows))
                for row in rows:
                    entry, = [line for line in entries if row["name"] in line]
                    self.assertIn(f"<#{targets[row['id']]['thread_id']}>", entry)
                    self.assertIn("尚缺总控核验回执", entry)
                for alias in {row["alias"] for row in rows}:
                    header, = [line for line in delivered.splitlines() if line.startswith("【") and alias in line]
                    self.assertIn(str(sum(row["alias"] == alias for row in rows)) + "任务", header)
                self.assertNotIn("未展示", delivered)

    def test_overview_overflow_reports_exact_omission_and_details_without_cut_lines(self):
        rows, targets = self.overview_tasks(180)
        conflict = dict(self.closed_task(), name="关闭后活动冲突必须优先显示", alias="ZZZ", status="待决策",
                        phase="关闭后出现pending，需总控reopen")
        targets[conflict["id"]] = {"thread_id": "1000000000000000999", "message_id": "1000000000000001999"}
        closed = dict(self.closed_task(), id="hidden", name="正常关闭不得展示")
        summary = board.overview([*rows, closed, conflict], targets, guild_id=TEST_GUILD, channel_id=TEST_CHANNEL)
        content = board.message_content("overview", summary)
        self.assertLessEqual(len(content.encode("utf-16-le")) // 2, 2000)
        self.assertEqual(content, summary + board.message_content("overview", ""))
        entries = [line for line in content.splitlines() if line.startswith("- ")]
        omitted = re.search(r"另有(\d+)项未展示", content)
        self.assertIsNotNone(omitted)
        self.assertEqual(len(entries) + int(omitted[1]), len(rows) + 1)
        self.assertIn(conflict["name"], entries[0])
        self.assertIn("关闭后出现pending", entries[0])
        self.assertNotIn(closed["name"], content)
        self.assertIn("明细", content)
        self.assertIn("线程列表", content)
        self.assertIn(f"<#{TEST_CHANNEL}>", content)
        for line in entries:
            self.assertEqual(len(re.findall(r"<#\d+>", line)), 1)
            self.assertTrue(any(row["name"] in line for row in [*rows, conflict]))

    def test_board_delivers_every_local_scale_task_entry_in_final_message(self):
        rows, _ = self.overview_tasks(17)
        self.api.next_id = 1000000000000000000
        board.Board(self.api, self.state).sync(rows)
        saved = json.loads(self.state.read_text())
        content = self.api.messages[saved["messages"]["overview"]["id"]]["content"]
        self.assertLessEqual(len(content.encode("utf-16-le")) // 2, 2000)
        # The same completeness contract now spans native cards and explicit pages.
        entries = [field for message in self.api.history(TEST_CHANNEL)
                   for embed in message.get("embeds", []) for field in embed.get("fields", [])
                   if field["value"].startswith("任务：")]
        self.assertEqual({field["value"].splitlines()[0] for field in entries}, {"任务：" + row["id"] for row in rows})
        for row in rows:
            entry = next(field for field in entries if field["value"].startswith("任务：" + row["id"] + "\n"))
            self.assertIn(row["name"], entry["name"])
            target = saved["tasks"][row["id"]]
            self.assertIn(f"https://discord.com/channels/{TEST_GUILD}/{target['thread_id']}/{target['message_id']}", entry["value"])
            self.assertIn("尚缺总控核验回执", entry["value"])
        self.assertTrue(self.api.messages[saved["messages"]["overview"]["id"]]["embeds"])
        self.assertTrue(content.endswith("`[hwb-board:" + board.digest("overview")[:24] + "]`"))

    def test_overview_serializer_refuses_to_silently_truncate_unbudgeted_content(self):
        with self.assertRaisesRegex(RuntimeError, "总览"):
            board.message_content("overview", "长" * 2000 + "<#1000000000000000999>")

    def test_overview_serializer_preserves_last_complete_entry_at_platform_limit(self):
        suffix = board.message_content("overview", "")
        entry = "\n- 尾部任务🔍 <#1000000000000000999>｜待总控核验；尚缺环境回执"
        available = 2000 - len((entry + suffix).encode("utf-16-le")) // 2
        summary = "中" * available + entry
        content = board.message_content("overview", summary)
        self.assertEqual(len(content.encode("utf-16-le")) // 2, 2000)
        self.assertEqual(content, summary + suffix)

    def test_overview_without_mapping_keeps_name_and_explicit_entry_blocker(self):
        content = board.message_content("overview", board.overview([task()], guild_id=TEST_GUILD))
        self.assertIn(task()["name"], content)
        self.assertIn("入口未映射", content)

    def test_overview_emoji_budget_counts_final_serialized_content(self):
        rows, targets = self.overview_tasks(40)
        for row in rows:
            row["name"] += "🔍" * 20
        summary = board.overview(rows, targets, guild_id=TEST_GUILD)
        content = board.message_content("overview", summary)
        self.assertLessEqual(len(content.encode("utf-16-le")) // 2, 2000)
        self.assertEqual(content, summary + board.message_content("overview", ""))
        self.assertIn("未展示", content)

    def test_overview_omits_normal_closed_tasks_and_their_timestamps(self):
        completed = dict(self.closed_task(), name="完成任务", alias="完成项目", updated="2099-01-01")
        cancelled = dict(completed, id="取消", name="取消任务", alias="取消项目", status="已取消")
        targets = {row["id"]: {"thread_id": "10", "message_id": "11"}
                   for row in (completed, cancelled, task())}
        text = board.overview([completed, cancelled, task()], targets, guild_id=TEST_GUILD)
        self.assertIn("开发任务", text)
        for hidden in ("完成项目", "取消项目", "完成任务", "取消任务", "2099-01-01"):
            self.assertNotIn(hidden, text)
        self.assertIn("暂无未关闭任务", board.overview([completed, cancelled], targets, guild_id=TEST_GUILD))

    def test_controller_disposition_is_visible_in_card_and_overview_without_changing_runtime(self):
        row = dict(task(), disposition="合入主线：等维护者合并；上线验收：等环境回执")
        board.Board(self.api, self.state).sync([row])
        saved = json.loads(self.state.read_text())
        for mid in (saved["messages"]["overview"]["id"], saved["tasks"][row["id"]]["message_id"]):
            text = json.dumps(self.api.messages[mid], ensure_ascii=False)
            self.assertIn("等维护者合并", text)
            self.assertIn("运行中", text)

    def test_closed_conflict_is_exposed_before_large_open_backlog(self):
        backlog = [dict(task(), id=f"open-{n}", alias="AAA", name=f"未关闭任务{n}") for n in range(40)]
        conflict = dict(self.closed_task(), alias="ZZZ", name="关闭后冲突任务", status="待决策", phase="关闭后出现pending")
        targets = {row["id"]: {"thread_id": "10", "message_id": "11"} for row in [*backlog, conflict]}
        text = board.message_content("overview", board.overview([*backlog, conflict], targets, guild_id=TEST_GUILD))
        self.assertIn("关闭后冲突任务", text)
        self.assertIn("关闭后出现pending", text)

    def test_archived_closed_conflict_remains_in_overview_and_preserves_history(self):
        board.Board(self.api, self.state).sync([task()])
        board.Board(self.api, self.state).sync([self.closed_task()])
        saved = json.loads(self.state.read_text())
        thread = saved["tasks"]["任务-1"]["thread_id"]
        history = copy.deepcopy(self.api.history(thread))
        for conflict in ({"pending": ["7"]}, {"worker_alive": True}):
            row = dict(self.closed_task(), status="待决策", phase="关闭后worker再次活动或有pending", **conflict)
            board.Board(self.api, self.state).sync([row])
            summary = self.api.messages[saved["messages"]["overview"]["id"]]["content"]
            self.assertIn("开发任务", summary)
            self.assertIn("关闭后", summary)
            self.assertIn(thread, self.api.archived)
            self.assertEqual(self.api.history(thread), history)

    def test_close_updates_original_card_and_overview_before_archiving(self):
        board.Board(self.api, self.state).sync([task()])
        prior = json.loads(self.state.read_text())
        mid = prior["tasks"]["任务-1"]["message_id"]
        tid = prior["tasks"]["任务-1"]["thread_id"]
        start = len(self.api.calls)
        board.Board(self.api, self.state).sync([self.closed_task()])
        current = json.loads(self.state.read_text())
        self.assertEqual(current["tasks"]["任务-1"]["message_id"], mid)
        self.assertIn(tid, self.api.archived)
        calls = self.api.calls[start:]
        archive_index = next(i for i,c in enumerate(calls) if c[0]=="PATCH" and c[1]==f"/channels/{tid}" and c[2]=={"archived":True})
        summary = prior["messages"]["overview"]["id"]
        self.assertTrue(any(c[0]=="PATCH" and c[1].endswith("/"+summary) for c in calls[:archive_index]))
        self.assertIn("已完成", self.api.messages[mid]["content"])

    def test_closed_periodic_sync_never_reopens_or_posts_again(self):
        board.Board(self.api, self.state).sync([task()])
        board.Board(self.api, self.state).sync([self.closed_task()])
        before = dict(self.api.operations)
        ids = set(self.api.messages)
        for _ in range(2):
            board.Board(self.api, self.state).sync([self.closed_task()])
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(self.api.operations[method], before.get(method,0))
        self.assertEqual(set(self.api.messages), ids)
        self.assertTrue(self.api.archived)

    def test_archive_ack_loss_recovers_without_duplicate_milestone(self):
        board.Board(self.api, self.state).sync([task()])
        self.api.archive_ack_lost = True
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([self.closed_task()])
        posts = self.api.operations["POST"]
        ids = set(self.api.messages)
        board.Board(self.api, self.state).sync([self.closed_task()])
        self.assertEqual(posts, self.api.operations["POST"])
        self.assertEqual(ids, set(self.api.messages))
        self.assertTrue(self.api.archived)

    def test_only_fresh_explicit_reopen_unarchives_once(self):
        board.Board(self.api, self.state).sync([task()])
        board.Board(self.api, self.state).sync([self.closed_task()])
        reopened = dict(task("待验收"), closed=False, control_head="reopen-2",
                        reopen_event={"hash":"reopen-2"}, control_event={"hash":"reopen-2", "action":"reopen", "reason":"总控要求继续", "approval_ref":"discord:reopen"})
        board.Board(self.api, self.state).sync([reopened])
        self.assertFalse(self.api.archived)
        before = dict(self.api.operations)
        board.Board(self.api, self.state).sync([reopened])
        self.assertEqual(before.get("POST",0), self.api.operations["POST"])
        self.assertEqual(before.get("PATCH",0), self.api.operations["PATCH"])
        tid = json.loads(self.state.read_text())["tasks"]["任务-1"]["thread_id"]
        self.api.archived.add(tid)
        board.Board(self.api, self.state).sync([reopened])
        self.assertIn(tid,self.api.archived)

    def test_manually_archived_open_thread_is_read_only_until_reopen(self):
        board.Board(self.api, self.state).sync([task()])
        tid = json.loads(self.state.read_text())["tasks"]["任务-1"]["thread_id"]
        self.api.archived.add(tid)
        before = self.api.operations["POST"]
        board.Board(self.api, self.state).sync([task()])
        self.assertIn(tid,self.api.archived)
        self.assertEqual(before,self.api.operations["POST"])

    def test_overview_failure_does_not_archive_before_completion_partition(self):
        board.Board(self.api, self.state).sync([task()])
        overview_id = json.loads(self.state.read_text())["messages"]["overview"]["id"]
        original = self.api.request
        def failed(method,path,body=None):
            if method=="PATCH" and path.endswith("/"+overview_id):
                raise RuntimeError("overview write failed")
            return original(method,path,body)
        with patch.object(self.api,"request",side_effect=failed):
            with self.assertRaises(RuntimeError):
                board.Board(self.api,self.state).sync([self.closed_task()])
        self.assertFalse(self.api.archived)
        posts = self.api.operations["POST"]
        board.Board(self.api,self.state).sync([self.closed_task()])
        self.assertTrue(self.api.archived)
        self.assertEqual(posts,self.api.operations["POST"])

    def test_closed_worker_conflict_does_not_archive_active_thread(self):
        board.Board(self.api,self.state).sync([task()])
        conflict=dict(self.closed_task(),status="待决策",phase="关闭后worker活动冲突")
        posts=self.api.operations["POST"]
        board.Board(self.api,self.state).sync([conflict])
        self.assertFalse(self.api.archived)
        self.assertEqual(posts,self.api.operations["POST"])

    def test_closed_archived_state_never_replays_historical_reopen(self):
        board.Board(self.api, self.state).sync([task()])
        closed=dict(self.closed_task(),reopen_event={"hash":"old-reopen"})
        board.Board(self.api,self.state).sync([closed])
        before=dict(self.api.operations)
        board.Board(self.api,self.state).sync([closed])
        self.assertTrue(self.api.archived)
        self.assertEqual(before["PATCH"],self.api.operations["PATCH"])

    def test_controlled_card_missing_fails_instead_of_replacing_message_id(self):
        board.Board(self.api,self.state).sync([task()])
        row=json.loads(self.state.read_text())["tasks"]["任务-1"]
        del self.api.messages[row["message_id"]]
        posts=self.api.operations["POST"]
        with self.assertRaises(RuntimeError):
            board.Board(self.api,self.state).sync([self.closed_task()])
        self.assertEqual(posts,self.api.operations["POST"])

    def test_real_discord_thread_lookup_never_unarchives(self):
        api=board.Discord.__new__(board.Discord)
        api.guild_id, api.channel_id = TEST_GUILD, TEST_CHANNEL
        api.bot,api.threads,api.thread_states="bot",set(),{}
        remote={"id":"10","parent_id":TEST_CHANNEL,"guild_id":TEST_GUILD,"type":11,
                "owner_id":"bot","thread_metadata":{"archived":True}}
        with patch.object(api,"request",return_value=remote) as request:
            self.assertEqual(api.thread("10","name"),"10")
            self.assertTrue(api.is_archived("10"))
            request.assert_called_once_with("GET","/channels/10")

    def test_real_archive_transport_writes_flag_and_checks_readback(self):
        api=board.Discord.__new__(board.Discord)
        api.guild_id, api.channel_id = TEST_GUILD, TEST_CHANNEL
        api.bot,api.threads,api.thread_states="bot",{"10"},{"10":False}
        remote={"id":"10","parent_id":TEST_CHANNEL,"guild_id":TEST_GUILD,
                "thread_metadata":{"archived":False}}
        def transport(method,path,body=None):
            if method=="PATCH":
                remote["thread_metadata"]["archived"]=body["archived"]
            return remote
        with patch.object(api,"request",side_effect=transport) as request:
            api.set_archived("10",True)
            self.assertTrue(api.is_archived("10"))
            request.assert_any_call("PATCH","/channels/10",{"archived":True})

    def seed_legacy(self, tasks):
        legacy = board.Board(self.api, self.state)
        old_ids = {}
        for row in tasks:
            tid = row["id"]
            mid = legacy.message("task:" + tid, TEST_CHANNEL, board.card(row))
            self.api.threads[mid] = row["alias"] + "｜" + row["name"]
            stage_mid = legacy.message("stage:" + tid + ":1", mid, "原有里程碑，必须保留")
            legacy.state["tasks"][tid] = {
                "sequence": 1, "message_id": mid, "thread_id": mid,
                "stage": {k: row[k] for k in ("status", "phase", "pending", "acceptance")}}
            old_ids[tid] = (mid, stage_mid)
        legacy.message("overview", TEST_CHANNEL, "旧总览")
        legacy.save()
        return old_ids

    def test_new_layout_parent_only_overview_and_thread_owns_card_and_milestone(self):
        board.Board(self.api, self.state).sync([task()])
        state = json.loads(self.state.read_text())
        entry = state["tasks"]["任务-1"]
        parent = self.api.history(TEST_CHANNEL)
        self.assertEqual([m["id"] for m in parent], [state["messages"]["overview"]["id"]])
        self.assertEqual(self.api.messages[entry["message_id"]]["channel_id"], entry["thread_id"])
        self.assertNotEqual(entry["message_id"], entry["thread_id"])
        self.assertIn(f"/{entry['thread_id']}/{entry['message_id']}", parent[0]["content"])
        self.assertTrue(all(m["channel_id"] == entry["thread_id"] for m in self.api.history(entry["thread_id"])))
        self.assertEqual(state["layout_version"], 2)

    def test_v1_migration_preserves_threads_and_history_and_only_deletes_old_cards(self):
        second = dict(task(), id="任务-2", name="另一个任务")
        tasks = [task(), second]
        olds = self.seed_legacy(tasks)
        unrelated = board.Board(self.api, self.state).message("unrelated", TEST_CHANNEL, "其他消息不得删除")
        summary = json.loads(self.state.read_text())["messages"]["overview"]["id"]
        start = len(self.api.calls)
        board.Board(self.api, self.state).sync(tasks)
        state = json.loads(self.state.read_text())
        for tid, (old, milestone) in olds.items():
            entry = state["tasks"][tid]
            self.assertEqual(entry["thread_id"], old)
            self.assertNotEqual(entry["message_id"], old)
            self.assertIn(milestone, self.api.messages)
            self.assertNotIn(old, self.api.messages)
            self.assertTrue(state["migration"][tid]["deleted"])
            self.assertEqual(state["migration"][tid]["history_before"], state["migration"][tid]["history_after"])
        self.assertEqual({m["id"] for m in self.api.history(TEST_CHANNEL)}, {summary, unrelated})
        calls = self.api.calls[start:]
        delete_index = next(i for i, c in enumerate(calls) if c[0] == "DELETE")
        for entry in state["tasks"].values():
            route = f"/channels/{entry['thread_id']}/messages/{entry['message_id']}"
            self.assertIn(("GET", route, None), calls[:delete_index])
        summary_edit = next(i for i, c in enumerate(calls) if c[0] == "PATCH" and c[1].endswith("/" + summary))
        self.assertLess(summary_edit, delete_index)

    def test_migration_delete_ack_loss_recovers_without_reposting(self):
        old, milestone = self.seed_legacy([task()])["任务-1"]
        self.api.lose_delete_reply = True
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([task()])
        self.assertNotIn(old, self.api.messages)
        posts = self.api.operations["POST"]
        board.Board(self.api, self.state).sync([task()])
        self.assertEqual(self.api.operations["POST"], posts)
        self.assertIn(milestone, self.api.messages)
        self.assertEqual(len(self.api.history(TEST_CHANNEL)), 1)
        before = dict(self.api.operations)
        board.Board(self.api, self.state).sync([task()])
        for method in ("POST", "PATCH", "DELETE", "PUT"):
            self.assertEqual(self.api.operations[method], before.get(method, 0))

    def test_foreign_legacy_starter_never_deleted(self):
        old, _ = self.seed_legacy([task()])["任务-1"]
        self.api.messages[old]["author"] = {"id": "human"}
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([task()])
        self.assertIn(old, self.api.messages)
        self.assertEqual(self.api.operations["DELETE"], 0)

    def test_missing_known_thread_never_replaced(self):
        old, milestone = self.seed_legacy([task()])["任务-1"]
        del self.api.threads[old]
        before = dict(self.api.operations)
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([task()])
        self.assertEqual(self.api.operations["POST"], before.get("POST", 0))
        self.assertIn(milestone, self.api.messages)
        self.assertIn(old, self.api.messages)

    def test_standalone_public_thread_recovers_lost_create_response(self):
        api = board.Discord.__new__(board.Discord)
        api.guild_id, api.channel_id = TEST_GUILD, TEST_CHANNEL
        api.bot, api.threads = "bot", set()
        api.thread_states = {}
        created = []

        def transport(method, path, body=None):
            if path == f"/guilds/{TEST_GUILD}/threads/active":
                return {"threads": created}
            if "/threads/archived/public" in path:
                return {"threads": [], "has_more": False}
            if method == "POST":
                self.assertEqual(path, f"/channels/{TEST_CHANNEL}/threads")
                self.assertEqual(body["type"], 11)
                created.append({"id": "independent", "type": 11, "guild_id": TEST_GUILD,
                                "parent_id": TEST_CHANNEL, "owner_id": "bot", "name": body["name"]})
                raise RuntimeError("create reply lost")
            raise AssertionError((method, path))

        with patch.object(api, "request", side_effect=transport):
            with self.assertRaises(RuntimeError):
                api.thread(None, "任务 ·hwb-unique")
            self.assertEqual(api.thread(None, "任务 ·hwb-unique"), "independent")
        self.assertEqual(len(created), 1)

    def test_idle_stopped_and_successful_turn_are_not_completion(self):
        for status in ("idle", "stopped"):
            result = board.classify({"status": status}, [], lambda _: True)
            self.assertEqual(result[0], "待总控核验")
        text = board.card(task("空闲待验收"))
        self.assertIn("开发完成：未核验", text)
        self.assertIn("合入主线：未核验", text)
        self.assertIn("上线验收：未核验", text)

    def test_running_pending_failed_and_dead_process(self):
        self.assertEqual(board.classify({"status": "running", "pid": 10}, [], lambda _: True)[0], "运行中")
        self.assertEqual(board.classify({"status": "idle", "pid": 10}, ["1"], lambda _: True)[0], "待决策")
        for meta in ({"status": "failed"}, {"status": "running"}, {"status": "running", "pid": 10}):
            self.assertEqual(board.classify(meta, [], lambda _: False)[0], "失败")
        self.assertEqual(board.classify({"status": "future"}, [])[0], "待决策")

    def test_second_poll_posts_and_edits_nothing_even_after_restart(self):
        board.Board(self.api, self.state).sync([task()])
        before = dict(self.api.operations)
        original = dict(self.api.messages)
        board.Board(self.api, self.state).sync([task()])
        self.assertEqual(self.api.operations["POST"], before["POST"])
        self.assertEqual(self.api.operations["PATCH"], before.get("PATCH", 0))
        self.assertEqual(self.api.operations["PUT"], before["PUT"])
        self.assertEqual(self.api.messages, original)
        self.assertEqual(len(self.api.threads), 1)

    def test_stage_change_edits_card_and_adds_one_thread_log(self):
        board.Board(self.api, self.state).sync([task()])
        state_before = json.loads(self.state.read_text())
        before_posts = self.api.operations["POST"]
        changed = task("空闲待验收")
        board.Board(self.api, self.state).sync([changed])
        after = json.loads(self.state.read_text())
        self.assertEqual(self.api.operations["POST"], before_posts + 1)
        self.assertEqual(state_before["tasks"]["任务-1"]["message_id"], after["tasks"]["任务-1"]["message_id"])
        board.Board(self.api, self.state).sync([changed])
        self.assertEqual(self.api.operations["POST"], before_posts + 1)

    def test_routine_evidence_update_only_edits_original_card(self):
        board.Board(self.api, self.state).sync([task()])
        before = self.api.operations["POST"]
        changed = task()
        changed["evidence"] += "，新事件"
        board.Board(self.api, self.state).sync([changed])
        self.assertEqual(self.api.operations["POST"], before)
        self.assertGreater(self.api.operations["PATCH"], 0)

    def test_lost_response_recovers_remote_message_without_duplicate(self):
        self.api.lose_reply = True
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([task()])
        self.assertEqual(len(self.api.messages), 1)
        board.Board(self.api, self.state).sync([task()])
        # overview + card + initial thread entry; lost POST was recovered by marker.
        self.assertEqual(len(self.api.messages), 3)

    def test_lost_local_state_recovers_messages_by_bot_and_marker(self):
        board.Board(self.api, self.state).sync([task()])
        self.state.unlink()
        before = self.api.operations["POST"]
        board.Board(self.api, self.state).sync([task()])
        self.assertEqual(self.api.operations["POST"], before)

    def assert_no_remote_writes(self, before):
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(self.api.operations[method], before.get(method, 0), method)

    def multi_stage_history(self):
        rows = [task(), task("待决策"), task("待验收")]
        for row in rows:
            board.Board(self.api, self.state).sync([row])
        return rows[-1], json.loads(self.state.read_text())

    def test_full_state_loss_recovers_all_stages_without_rewriting_history(self):
        latest, saved = self.multi_stage_history()
        original = copy.deepcopy(self.api.messages)
        before = dict(self.api.operations)
        self.state.unlink()
        board.Board(self.api, self.state).sync([latest])
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)
        recovered = json.loads(self.state.read_text())
        self.assertEqual(recovered["tasks"]["任务-1"]["sequence"], 3)
        for n in range(1, 4):
            key = f"stage:任务-1:{n}"
            self.assertEqual(recovered["messages"][key]["id"], saved["messages"][key]["id"])
        board.Board(self.api, self.state).sync([task("失败")])
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["任务-1"]["sequence"], 4)
        for mid, message in original.items():
            if mid != saved["tasks"]["任务-1"]["message_id"] and mid != saved["messages"]["overview"]["id"]:
                self.assertEqual(self.api.messages[mid], message)

    def test_closed_archived_full_state_loss_restores_original_delivery_heads_read_only(self):
        self.multi_stage_history()
        closed = dict(self.closed_task(), reopen_event={"hash": "historical-reopen"})
        board.Board(self.api, self.state).sync([closed])
        saved = json.loads(self.state.read_text())
        original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
        self.state.unlink()
        for _ in range(2):
            board.Board(self.api, self.state).sync([closed])
        recovered = json.loads(self.state.read_text())
        row, prior = recovered["tasks"]["任务-1"], saved["tasks"]["任务-1"]
        for key in ("thread_id", "message_id", "card_channel_id", "sequence", "stage",
                    "applied_control_head", "archived_control_head"):
            self.assertEqual(row.get(key), prior[key], key)
        self.assertNotIn("sync_error", row)
        self.assertIn(row["thread_id"], self.api.archived)
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)

    def test_legacy_hashed_milestones_recover_without_new_receipts(self):
        latest, _ = self.multi_stage_history()
        for message in self.api.messages.values():
            # Simulate messages from the deployed version with no recovery footer.
            lines = message["content"].splitlines()
            message["content"] = "\n".join(line for line in lines if not line.startswith("HWB阶段记录："))
        original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
        self.state.unlink()
        board.Board(self.api, self.state).sync([latest])
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["任务-1"]["sequence"], 3)
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)

    def test_partial_state_sequence_reset_recovers_remote_maximum(self):
        latest, saved = self.multi_stage_history()
        saved["tasks"]["任务-1"].update(sequence=0)
        saved["tasks"]["任务-1"].pop("stage")
        board.atomic_json(self.state, saved)
        original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
        board.Board(self.api, self.state).sync([latest])
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["任务-1"]["sequence"], 3)
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)

    def test_legacy_remote_stage_overrides_damaged_cached_stage(self):
        latest, saved = self.multi_stage_history()
        for key, cached in saved["messages"].items():
            if key.startswith("stage:"):
                message = self.api.messages[cached["id"]]
                message["content"] = "\n".join(line for line in message["content"].splitlines()
                                               if not line.startswith("HWB阶段记录："))
                cached["hash"] = board.digest(message["content"])
        saved["tasks"]["任务-1"]["stage"]["phase"] = "损坏的本地缓存"
        board.atomic_json(self.state, saved)
        original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
        board.Board(self.api, self.state).sync([latest])
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["任务-1"]["sequence"], 3)
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)

    def test_cached_sequence_without_remote_stages_fails_closed(self):
        latest, saved = self.multi_stage_history()
        for key, cached in saved["messages"].items():
            if key.startswith("stage:"):
                del self.api.messages[cached["id"]]
        saved["messages"] = {}
        board.atomic_json(self.state, saved)
        original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([latest])
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)

    def test_legacy_closed_archived_recovery_and_local_save_failure_are_read_only(self):
        board.Board(self.api, self.state).sync([task()])
        board.Board(self.api, self.state).sync([self.closed_task()])
        saved = json.loads(self.state.read_text())
        for message in self.api.messages.values():
            message["content"] = "\n".join(line for line in message["content"].splitlines()
                                           if not line.startswith("HWB阶段记录："))
        original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
        self.state.unlink()
        with patch.object(board, "atomic_json", side_effect=OSError("state write failed")):
            with self.assertRaises(OSError):
                board.Board(self.api, self.state).sync([self.closed_task()])
        for _ in range(2):
            board.Board(self.api, self.state).sync([self.closed_task()])
        row = json.loads(self.state.read_text())["tasks"]["任务-1"]
        self.assertEqual(row["message_id"], saved["tasks"]["任务-1"]["message_id"])
        self.assertEqual(row["archived_control_head"], "close-1")
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)

    def test_unknown_duplicate_or_gapped_stage_history_fails_closed_before_writes(self):
        for damage in ("unknown", "duplicate", "gap", "foreign", "unreadable"):
            with self.subTest(damage=damage):
                self.api = FakeDiscord()
                self.state.unlink(missing_ok=True)
                latest, saved = self.multi_stage_history()
                mid = saved["messages"]["stage:任务-1:2"]["id"]
                if damage == "gap":
                    del self.api.messages[mid]
                elif damage == "foreign":
                    self.api.messages[mid]["author"] = {"id": "human"}
                elif damage == "unreadable":
                    marker = self.api.messages[mid]["content"].splitlines()[-1]
                    self.api.messages[mid]["content"] = "旧阶段无法解读\n" + marker
                else:
                    clone = copy.deepcopy(self.api.messages[mid])
                    clone["id"] = "9999"
                    if damage == "unknown":
                        clone["content"] = "未知记录\n`[hwb-board:" + board.digest("stage:任务-1:999")[:24] + "]`"
                    self.api.messages[clone["id"]] = clone
                self.state.unlink()
                original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
                with self.assertRaises(RuntimeError):
                    board.Board(self.api, self.state).sync([latest])
                self.assertEqual(self.api.messages, original)
                self.assert_no_remote_writes(before)

    def test_closed_missing_original_thread_never_creates_replacement(self):
        before = dict(self.api.operations)
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([self.closed_task()])
        self.assertEqual(self.api.threads, {})
        self.assertEqual(self.api.messages, {})
        self.assert_no_remote_writes(before)

    def test_archived_close_recovery_rejects_missing_or_conflicting_evidence(self):
        for damage in ("missing-card", "wrong-card", "wrong-head", "wrong-phase", "missing-stage"):
            with self.subTest(damage=damage):
                self.api = FakeDiscord()
                self.state.unlink(missing_ok=True)
                board.Board(self.api, self.state).sync([task()])
                board.Board(self.api, self.state).sync([self.closed_task()])
                saved = json.loads(self.state.read_text())
                mid = saved["tasks"]["任务-1"]["message_id"]
                stage = saved["messages"]["stage:任务-1:2"]["id"]
                mutations = {
                    "missing-card": lambda: self.api.messages.pop(mid),
                    "wrong-card": lambda: self.api.messages[mid].update(
                        content=self.api.messages[mid]["content"].replace("discord:approved", "discord:other")),
                    "wrong-head": lambda: self.api.messages[stage].update(
                        content=self.api.messages[stage]["content"].replace("close-1", "other-close")),
                    "wrong-phase": lambda: self.api.messages[stage].update(
                        content=self.api.messages[stage]["content"].replace("已完成｜已完成", "已完成｜未知阶段").replace('"phase":"已完成"', '"phase":"未知阶段"')),
                    "missing-stage": lambda: self.api.messages.pop(stage),
                }
                mutations[damage]()
                self.state.unlink()
                original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
                with self.assertRaises(RuntimeError):
                    board.Board(self.api, self.state).sync([self.closed_task()])
                self.assertEqual(self.api.messages, original)
                self.assert_no_remote_writes(before)
                self.assertTrue(self.api.archived)

    def test_existing_milestone_is_immutable_even_when_caller_reuses_key(self):
        _, saved = self.multi_stage_history()
        key = "stage:任务-1:1"
        original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).message(key, saved["tasks"]["任务-1"]["thread_id"], "不能覆盖历史")
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)

    def test_stage_ack_loss_then_new_progress_preserves_committed_milestone(self):
        board.Board(self.api, self.state).sync([task()])
        self.api.lose_reply = True
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([task("待决策")])
        committed = copy.deepcopy(self.api.messages)
        board.Board(self.api, self.state).sync([task("待验收")])
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["tasks"]["任务-1"]["sequence"], 3)
        old_stage_ids = {m["id"] for m in committed.values() if "[hwb-board:" in m["content"] and "待决策编号：" in m["content"]}
        self.assertEqual(len(old_stage_ids), 2)
        for mid in old_stage_ids:
            self.assertEqual(self.api.messages[mid], committed[mid])

    def test_deleted_card_recreated_inside_same_thread(self):
        board.Board(self.api, self.state).sync([task()])
        initial = json.loads(self.state.read_text())["tasks"]["任务-1"]
        old = initial["message_id"]
        del self.api.messages[old]
        board.Board(self.api, self.state).sync([task()])
        new = json.loads(self.state.read_text())["tasks"]["任务-1"]["message_id"]
        self.assertNotEqual(old, new)
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["任务-1"]["thread_id"], initial["thread_id"])
        self.assertEqual(self.api.messages[new]["channel_id"], initial["thread_id"])

    def test_foreign_message_is_not_edited(self):
        board.Board(self.api, self.state).sync([task()])
        mid = json.loads(self.state.read_text())["messages"]["overview"]["id"]
        self.api.messages[mid]["author"] = {"id": "other-user"}
        with self.assertRaises(RuntimeError):
            board.Board(self.api, self.state).sync([task()])

    def test_pin_denied_does_not_block_cards_and_recovers_when_granted(self):
        original = self.api.request

        def denied(method, path, body=None):
            if method == "PUT":
                raise board.DiscordError(403)
            return original(method, path, body)

        with patch.object(self.api, "request", side_effect=denied):
            board.Board(self.api, self.state).sync([task()])
        state = json.loads(self.state.read_text())
        self.assertIn("PIN_MESSAGES", state["pin_error"])
        self.assertIn("任务-1", state["tasks"])
        posts = self.api.operations["POST"]
        board.Board(self.api, self.state).sync([task()])
        self.assertNotIn("pin_error", json.loads(self.state.read_text()))
        self.assertEqual(self.api.operations["POST"], posts)

    def test_scans_hwb_decisions_and_complete_event_lines_only(self):
        directory = self.root / "workers" / "任务-1"
        (directory / "decisions").mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps({"id": "任务-1", "tool": "codex", "status": "idle",
                                                         "name": "示例项目-任务", "cwd": str(self.root / "synthetic-project"), "updated": "2026-10-22 00:00:00"}))
        (directory / "decisions" / "1.json").write_text('{"secret":"never publish"}')
        (directory / "events.jsonl").write_text('{"kind":"turn_done","t":"2026-10-22 00:01:00","text":"secret"}\n{"kind":')
        result = board.scan_tasks(self.root)[0]
        self.assertEqual(result["status"], "待决策")
        self.assertEqual(result["alias"], "示例项目")
        self.assertNotIn("secret", board.card(result))
        (directory / "decisions" / "1.answer.json").write_text('{}')
        self.assertEqual(board.scan_tasks(self.root)[0]["status"], "待总控核验")

    def test_atomic_state_and_invalid_state_fail_closed(self):
        board.atomic_json(self.state, {"中文": 1})
        self.assertEqual(json.loads(self.state.read_text()), {"中文": 1})
        self.state.write_text("{")
        with self.assertRaises(json.JSONDecodeError):
            board.Board(self.api, self.state)

    def test_lock_contention_exits_before_discord_access(self):
        import fcntl

        with (self.root / "sync.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("sys.argv", ["board", "--home", str(self.root), "--state-dir", str(self.root), "--guild-id", TEST_GUILD, "--channel-id", TEST_CHANNEL]), patch.object(board, "Discord") as api:
                board.main()
                api.assert_not_called()

    def test_network_scope_rejects_other_channel_and_wrong_guild(self):
        with patch.object(board.Discord, "request", return_value={"guild_id": "other", "type": 0}):
            with self.assertRaises(RuntimeError):
                board.Discord("unused", TEST_GUILD, TEST_CHANNEL)
        api = board.Discord.__new__(board.Discord)
        api.guild_id, api.channel_id = TEST_GUILD, TEST_CHANNEL
        api.threads = set()
        with patch.object(board.urllib.request, "urlopen") as network:
            with self.assertRaises(RuntimeError):
                api.request("POST", "/channels/999/messages", {"content": "test"})
            network.assert_not_called()

    def cli_args(self, *extra):
        return ["board", "--home", str(self.root), "--state-dir", str(self.root), *extra]

    def test_missing_target_cli_stops_before_secrets_state_or_network(self):
        for extra in ([], ["--guild-id", TEST_GUILD], ["--channel-id", TEST_CHANNEL]):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), \
                    patch("sys.argv", self.cli_args(*extra)), \
                    patch.object(board, "Discord") as api, patch.object(board, "token_from_home") as token:
                with self.assertRaises(SystemExit) as error:
                    board.main()
                self.assertEqual(error.exception.code, 2)
                api.assert_not_called()
                token.assert_not_called()
                self.assertFalse((self.root / "sync.lock").exists())

    def test_invalid_target_values_stop_before_network(self):
        invalid = [None, True, 12, "", "0", "-1", "abc", " 100000000000000001", "010000000000000001",
                   "100000000000000001/threads", "100000000000000001,100000000000000002", str(2**64)]
        for value in invalid:
            for guild, channel in ((value, TEST_CHANNEL), (TEST_GUILD, value), (TEST_GUILD, TEST_GUILD)):
                with self.subTest(guild=guild, channel=channel), patch.object(board.urllib.request, "urlopen") as network:
                    with self.assertRaises(ValueError):
                        board.Discord("synthetic-token", guild, channel)
                    network.assert_not_called()

    def test_invalid_cli_target_stops_before_secrets_or_directory_creation(self):
        destination = self.root / "must-not-create"
        args = ["board", "--home", str(self.root), "--state-dir", str(destination),
                "--guild-id", TEST_GUILD, "--channel-id", "all"]
        with patch("sys.argv", args), patch.object(board, "Discord") as api, \
                patch.object(board, "token_from_home") as token:
            with self.assertRaises(ValueError):
                board.main()
            api.assert_not_called()
            token.assert_not_called()
        self.assertFalse(destination.exists())

    def test_cli_explicit_targets_reach_transport_and_bind_state(self):
        args = self.cli_args("--guild-id", TEST_GUILD, "--channel-id", TEST_CHANNEL)
        with patch("sys.argv", args), patch("sys.path", [str(Path(SPEC.origin).parent), *sys.path]), \
                patch.object(board, "Discord", return_value=self.api) as api, \
                patch.object(board, "token_from_home", return_value="synthetic-token"):
            board.main()
        api.assert_called_once_with("synthetic-token", TEST_GUILD, TEST_CHANNEL)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["target"], {"guild_id": TEST_GUILD, "channel_id": TEST_CHANNEL})
        self.assertEqual(state["messages"]["overview"]["channel"], TEST_CHANNEL)

    def test_cli_auto_closes_existing_acceptance_but_dry_run_never_writes(self):
        with patch("sys.path", [str(Path(SPEC.origin).parent), *sys.path]):
            import dev_progress_review as review
        directory = self.root / "workers" / "task-1"
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps({"id": "task-1", "tool": "codex", "status": "stopped"}))
        log = review.ReviewLog(self.root / "control.json")
        board.Board(self.api, self.state).sync(review.project_tasks(board.scan_tasks(self.root), log))
        saved = json.loads(self.state.read_text())
        checks = {key: {"status": "passed", "evidence": ["artifact:" + key]} for key in review.STAGES}
        with patch.object(review, "require_controller"):
            log.apply(self.root, "task-1", "review", {"checks": checks}, controller="总控测试",
                      approval_ref="test:approval", board_state=saved)
        args = self.cli_args("--guild-id", TEST_GUILD, "--channel-id", TEST_CHANNEL)
        before = self.state.read_bytes(), log.path.read_bytes()
        with patch("sys.argv", [*args, "--dry-run"]), redirect_stdout(io.StringIO()), \
                patch.object(board, "Discord") as api, patch.object(review, "require_controller") as guard:
            board.main()
            api.assert_not_called()
            guard.assert_not_called()
        self.assertEqual((self.state.read_bytes(), log.path.read_bytes()), before)
        with patch("sys.argv", args), patch.object(review, "require_controller", side_effect=PermissionError("worker")), \
                patch.object(board, "Discord") as api:
            with self.assertRaises(PermissionError):
                board.main()
            api.assert_not_called()
        with patch("sys.argv", args), patch.object(review, "require_controller"), \
                patch.object(board, "Discord", return_value=self.api), \
                patch.object(board, "token_from_home", return_value="synthetic"):
            board.main()
        after = json.loads(self.state.read_text())
        self.assertEqual(saved["tasks"]["task-1"]["message_id"], after["tasks"]["task-1"]["message_id"])
        self.assertIn(after["tasks"]["task-1"]["thread_id"], self.api.archived)
        self.assertEqual(len(review.ReviewLog(log.path).events), 2)
        summary = self.api.messages[after["messages"]["overview"]["id"]]["content"]
        self.assertIn("暂无未关闭任务", summary)

    def test_configured_transport_validates_remote_parent_identity(self):
        expected = {"id": TEST_CHANNEL, "guild_id": TEST_GUILD, "type": 0}
        for bad in ({"id": "100000000000000003"}, {"guild_id": "100000000000000004"}, {"type": 11}):
            with self.subTest(bad=bad), patch.object(board.Discord, "request", return_value={**expected, **bad}):
                with self.assertRaises(RuntimeError):
                    board.Discord("synthetic-token", TEST_GUILD, TEST_CHANNEL)

    def test_configured_transport_never_expands_write_scope(self):
        def identity(method, path, body=None):
            if path == f"/channels/{TEST_CHANNEL}":
                return {"id": TEST_CHANNEL, "guild_id": TEST_GUILD, "type": 0}
            return {"id": "synthetic-bot"}
        with patch.object(board.Discord, "request", side_effect=identity):
            api = board.Discord("synthetic-token", TEST_GUILD, TEST_CHANNEL)
        paths = [("POST", "/channels/100000000000000003/messages"),
                 ("GET", "/guilds/100000000000000004/threads/active"),
                 ("POST", f"/guilds/{TEST_GUILD}/threads/active"), ("PATCH", "/users/@me")]
        with patch.object(board.urllib.request, "urlopen") as network:
            for method, path in paths:
                with self.subTest(method=method, path=path), self.assertRaises(RuntimeError):
                    api.request(method, path, {"content": "must not send"})
            network.assert_not_called()

    def test_target_binding_rejects_retargeted_existing_state(self):
        board.Board(self.api, self.state).sync([task()])
        before = self.state.read_bytes()
        other = FakeDiscord("100000000000000003", "100000000000000004")
        with self.assertRaises(ValueError):
            board.Board(other, self.state)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(other.operations, {})

    def test_separate_configured_boards_keep_parent_and_links_independent(self):
        other = FakeDiscord("100000000000000003", "100000000000000004")
        for api, state in ((self.api, self.state), (other, self.root / "other-state.json")):
            board.Board(api, state).sync([task()])
            saved = json.loads(state.read_text())
            overview = api.messages[saved["messages"]["overview"]["id"]]
            self.assertEqual(overview["channel_id"], api.channel_id)
            self.assertIn(f"https://discord.com/channels/{api.guild_id}/{saved['tasks']['任务-1']['thread_id']}/", overview["content"])
        before = dict(self.api.operations)
        board.Board(self.api, self.state).sync([task()])
        self.assert_no_remote_writes(before)

    def test_legacy_state_binds_same_parent_without_rewriting_messages(self):
        board.Board(self.api, self.state).sync([task()])
        saved = json.loads(self.state.read_text())
        saved.pop("target", None)
        board.atomic_json(self.state, saved)
        original, before = copy.deepcopy(self.api.messages), dict(self.api.operations)
        other = FakeDiscord(TEST_GUILD, "100000000000000003")
        with self.assertRaises(ValueError):
            board.Board(other, self.state)
        board.Board(self.api, self.state).sync([task()])
        recovered = json.loads(self.state.read_text())
        self.assertEqual(recovered["target"], {"guild_id": TEST_GUILD, "channel_id": TEST_CHANNEL})
        self.assertEqual(recovered["tasks"], saved["tasks"])
        self.assertEqual(self.api.messages, original)
        self.assert_no_remote_writes(before)

    def test_invalid_thread_lookup_does_not_authorize_foreign_writes(self):
        api = board.Discord.__new__(board.Discord)
        api.guild_id, api.channel_id = TEST_GUILD, TEST_CHANNEL
        api.bot, api.threads, api.thread_states = "bot", set(), {}
        remote = {"id": "100000000000000003", "parent_id": "100000000000000004", "guild_id": TEST_GUILD,
                  "type": 11, "owner_id": "bot"}
        with patch.object(api, "request", return_value=remote):
            with self.assertRaises(RuntimeError):
                api.thread(remote["id"], "untrusted")
        self.assertNotIn(remote["id"], api.threads)
        with patch.object(board.urllib.request, "urlopen") as network:
            with self.assertRaises(RuntimeError):
                api.request("POST", f"/channels/{remote['id']}/messages", {"content": "must not send"})
            network.assert_not_called()

    def test_deployment_wrapper_requires_new_flags_and_preserves_legacy_journal_in_dry_run(self):
        board.Board(self.api, self.state).sync([task()])
        saved = json.loads(self.state.read_text())
        saved.pop("target", None)
        board.atomic_json(self.state, saved)
        original = self.state.read_bytes()
        base = [sys.executable, SPEC.origin, "--home", str(self.root), "--state-dir", str(self.root)]
        wrapper = self.root / "sync.sh"
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(base) + ' "$@"\n')
        old = subprocess.run(["/bin/sh", str(wrapper), "--dry-run"], capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(old.returncode, 2)
        flags = ["--guild-id", TEST_GUILD, "--channel-id", TEST_CHANNEL]
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(base + flags) + ' "$@"\n')
        migrated = subprocess.run(["/bin/sh", str(wrapper), "--dry-run"], capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.assertEqual(json.loads(migrated.stdout), [])
        self.assertEqual(self.state.read_bytes(), original)
        self.assertFalse((self.root / "control.json").exists())


if __name__ == "__main__":
    unittest.main()
