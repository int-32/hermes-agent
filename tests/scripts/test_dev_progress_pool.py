"""Durable discovery and two-source tasks, independent of delivery and worker claims."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import dev_progress_pool as pool
import dev_progress_review as review
from test_dev_progress_board import FakeDiscord, board, task


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / "pool.json"
        self.log = pool.Pool(self.path)
        self.guard = patch.object(review, "require_controller")
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.number = 0

    def apply(self, action, payload, actor="controller", request_id=None):
        self.number += 1
        return self.log.apply(self.home, {
            "request_id": request_id or f"request-{self.number}", "action": action,
            "actor": actor, "evidence": ["artifact:raw"], "payload": payload})

    def discover(self, key="disk", run="run-1"):
        return self.apply("discover", {"source": "ops_inspection", "source_ref": run,
            "project": "project", "fingerprint": key, "title": "日志异常，尚待核实",
            "severity": "P2", "observed_at": "2026-10-24T01:00:00+08:00"}, "ops")

    def goal(self, tid="t1"):
        return self.apply("task", {"id": tid, "source": "approved_goal", "project": "project",
            "title": "批准的目标", "objective": "解决具体问题", "scope": "仅本地",
            "acceptance": "相关测试及总控读回", "approval_ref": "approval:goal", "issue_ids": []})

    def window(self, tid="t1", session="session-1"):
        return self.apply("window", {"task_id": tid, "kind": "native", "profile": "ops",
            "session_id": session, "executor": "ops", "url": "https://discord.com/channels/100/200/300"})

    def receipt(self, wid, kind, tid="t1"):
        return self.apply("receipt", {"task_id": tid, "window_id": wid, "kind": kind,
            "progress": "原始材料已登记", "next_step": "由总控验收"}, "ops")

    def test_repeat_inspection_dedupes_issue_but_keeps_observations_and_no_findings(self):
        self.discover()
        self.discover(run="run-2")
        self.apply("inspection", {"source_ref": "run-clean", "project": "project",
            "observed_at": "2026-10-24T02:00:00+08:00", "summary": "本次未发现问题"}, "ops")
        loaded = pool.Pool(self.path)
        self.assertEqual(len(loaded.data["issues"]), 1)
        issue = next(iter(loaded.data["issues"].values()))
        self.assertEqual(issue["state"], "unverified")
        self.assertEqual(len(issue["observations"]), 2)
        self.assertEqual(len(loaded.data["inspections"]), 1)
        self.assertFalse(loaded.data["tasks"])

    def test_idempotent_request_conflict_and_corrupt_chain_fail_closed(self):
        first = self.goal()
        request = copy.deepcopy(first["request"])
        self.assertEqual(self.log.apply(self.home, request), first)
        request["payload"]["scope"] = "different"
        with self.assertRaises(ValueError):
            self.log.apply(self.home, request)
        obj = json.loads(self.path.read_text())
        obj["events"][0]["request"]["payload"]["scope"] = "tampered"
        self.path.write_text(json.dumps(obj))
        with self.assertRaises(ValueError):
            pool.Pool(self.path)

    def test_many_to_many_links_and_sources_require_confirmed_issue_or_approval(self):
        self.discover()
        iid = next(iter(self.log.data["issues"]))
        payload = {"id": "ops-fix", "source": "ops_inspection", "project": "project",
            "title": "运维自处理", "objective": "修复", "scope": "本地", "acceptance": "读回",
            "approval_ref": "approval:ops", "issue_ids": [iid]}
        with self.assertRaises(ValueError):
            self.apply("task", payload)
        self.apply("issue_state", {"issue_id": iid, "state": "ready", "reason": "人工核实"})
        self.apply("task", payload)
        self.goal()
        self.apply("link", {"issue_id": iid, "task_id": "t1"})
        self.discover("other")
        second = next(i for i in self.log.data["issues"] if i != iid)
        self.apply("link", {"issue_id": second, "task_id": "t1"})
        self.assertEqual(set(self.log.data["tasks"]["t1"]["issue_ids"]), {iid, second})
        self.assertEqual(set(self.log.data["issues"][iid]["task_ids"]), {"t1", "ops-fix"})
        bad = dict(payload, id="bad", source="approved_goal", approval_ref="")
        with self.assertRaises(ValueError):
            self.apply("task", bad)

    def test_assignment_is_not_acceptance_and_report_cannot_skip_receipt(self):
        self.goal()
        self.window()
        wid = next(iter(self.log.data["windows"]))
        self.assertEqual(self.log.task_status("t1"), "ready")
        with self.assertRaises(ValueError):
            self.receipt(wid, "report")
        self.receipt(wid, "accepted")
        self.assertEqual(self.log.task_status("t1"), "executing")
        self.receipt(wid, "report")
        self.assertEqual(self.log.task_status("t1"), "awaiting_review")
        self.assertFalse(self.log.data["tasks"]["t1"]["closed"])

    def test_worker_cannot_create_goal_review_close_or_change_issue_disposition(self):
        self.guard.stop()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "worker"}):
            self.discover()
            with self.assertRaises(PermissionError):
                self.goal()
            iid = next(iter(self.log.data["issues"]))
            with self.assertRaises(PermissionError):
                self.apply("issue_state", {"issue_id": iid, "state": "ready", "reason": "claim"})
        self.guard.start()

    def test_shared_window_reused_counts_are_independent_and_receipts_are_per_task(self):
        self.goal()
        self.goal("t2")
        self.window()
        self.window("t2")
        self.assertEqual(len(self.log.data["windows"]), 1)
        wid = next(iter(self.log.data["windows"]))
        self.receipt(wid, "accepted")
        self.assertEqual(self.log.task_status("t2"), "ready")
        self.assertEqual(self.log.counts([]), {"issues": 0, "tasks": 2, "windows": 1})

    def test_three_layer_review_bound_to_current_work_and_explicit_reopen(self):
        self.goal()
        self.window()
        wid = next(iter(self.log.data["windows"]))
        self.receipt(wid, "accepted")
        self.receipt(wid, "report")
        close = {"task_id": "t1", "approval_ref": "approval:close", "reason": "总控核验", "outcome": "completed"}
        with self.assertRaises(ValueError):
            self.apply("close", close)
        checks = {k: {"status": "passed", "evidence": ["raw:" + k]} for k in review.STAGES}
        self.apply("review", {"task_id": "t1", "approval_ref": "approval:review", "checks": checks})
        self.receipt(wid, "report")
        with self.assertRaises(ValueError):
            self.apply("close", close)
        self.apply("review", {"task_id": "t1", "approval_ref": "approval:review-2", "checks": checks})
        self.apply("close", close)
        with self.assertRaises(ValueError):
            self.receipt(wid, "accepted")
        self.apply("reopen", {"task_id": "t1", "approval_ref": "approval:reopen", "reason": "继续核验"})
        self.assertEqual(self.log.task_status("t1"), "ready")
        self.assertIsNone(self.log.data["tasks"]["t1"]["review"])

    def test_duplicate_deferred_and_issue_close_need_reason_and_no_unclosed_tasks(self):
        self.discover()
        first = next(iter(self.log.data["issues"]))
        self.discover("other")
        second = next(i for i in self.log.data["issues"] if i != first)
        with self.assertRaises(ValueError):
            self.apply("issue_state", {"issue_id": second, "state": "deferred", "reason": ""})
        self.apply("issue_state", {"issue_id": second, "state": "duplicate", "reason": "同根因", "duplicate_of": first})
        with self.assertRaises(ValueError):
            self.apply("issue_state", {"issue_id": first, "state": "duplicate", "reason": "环", "duplicate_of": second})
        self.goal()
        self.apply("link", {"issue_id": first, "task_id": "t1"})
        with self.assertRaises(ValueError):
            self.apply("issue_state", {"issue_id": first, "state": "closed", "reason": "不能提前关闭"})

    def test_board_delivery_recovers_state_and_repeats_without_writes(self):
        self.discover()
        self.goal()
        self.window()
        api = FakeDiscord()
        state = self.home / "state.json"
        board.Board(api, state).sync([task()], pool=self.log)
        ids = set(api.messages)
        writes = {m: api.operations[m] for m in ("POST", "PATCH", "PUT", "DELETE")}
        board.Board(api, state).sync([task()], pool=pool.Pool(self.path))
        self.assertEqual(writes, {m: api.operations[m] for m in writes})
        state.unlink()
        board.Board(api, state).sync([task()], pool=pool.Pool(self.path))
        self.assertEqual(ids, set(api.messages))
        overview = next(m["content"] for m in api.messages.values() if "问题 1｜任务 2｜执行窗口 2" in m["content"])
        self.assertIn("问题发现入口", overview)
        self.assertIn("https://discord.com/channels/100/200/300", "\n".join(m["content"] for m in api.messages.values()))
        saved = json.loads(state.read_text())
        self.path.unlink()
        with self.assertRaises(ValueError):
            pool.Pool(self.path).validate_anchor(saved)

    def test_large_pool_all_items_delivered_without_silent_truncation(self):
        for i in range(25):
            self.discover(key=f"disk-{i}")
        api = FakeDiscord()
        board.Board(api, self.home / "state.json").sync([], pool=self.log)
        contents = "\n".join(m["content"] for m in api.messages.values())
        for iid in self.log.data["issues"]:
            self.assertIn(iid, contents)
        self.assertTrue(all(len(m["content"]) <= 2000 for m in api.messages.values()))

    def test_cli_intake_and_snapshot_real_subprocess_without_secrets_or_network(self):
        script = Path(pool.__file__)
        event = {"request_id": "clean-run", "action": "inspection", "actor": "ops", "evidence": ["artifact:clean"],
            "payload": {"source_ref": "run-1", "project": "project", "observed_at": "2026-10-24T00:00:00Z", "summary": "无问题"}}
        incoming = self.home / "incoming.json"
        incoming.write_text(json.dumps(event))
        base = [sys.executable, str(script), "--home", str(self.home), "--state-dir", str(self.home)]
        env = {**os.environ, "CODEX_THREAD_ID": "worker"}
        result = subprocess.run(base + ["apply", "--file", str(incoming)], env=env, capture_output=True, text=True, check=False, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(base + ["snapshot"], env=env, capture_output=True, text=True, check=False, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["inspections"]), 1)
        self.assertFalse((self.home / ".env").exists())

    def test_repeated_source_run_does_not_duplicate_observations_or_clean_inspections(self):
        self.discover()
        self.discover()
        self.assertEqual(len(next(iter(self.log.data["issues"].values()))["observations"]), 1)
        run = {"source_ref": "run-clean", "project": "project", "observed_at": "2026-10-24T02:00:00Z", "summary": "无问题"}
        self.apply("inspection", run, "ops")
        self.apply("inspection", run, "ops")
        self.assertEqual(len(self.log.data["inspections"]), 1)
        with self.assertRaises(ValueError):
            self.apply("inspection", dict(run, summary="changed"), "ops")

    def test_missing_pool_and_state_detected_from_remote_before_any_write(self):
        self.goal()
        api = FakeDiscord()
        state = self.home / "state.json"
        board.Board(api, state).sync([task()], pool=self.log)
        self.path.unlink()
        state.unlink()
        before = {m: api.operations[m] for m in ("POST", "PATCH", "PUT", "DELETE")}
        with self.assertRaises(ValueError):
            board.Board(api, state).sync([task("待验收")], pool=pool.Pool(self.path))
        self.assertEqual(before, {m: api.operations[m] for m in before})

    def test_pool_message_ack_loss_and_local_ack_loss_recover_without_duplicates(self):
        self.discover()
        api = FakeDiscord()
        api.lose_reply = True
        state = self.home / "state.json"
        with self.assertRaises(RuntimeError):
            board.Board(api, state).sync([], pool=self.log)
        posted = set(api.messages)
        board.Board(api, state).sync([], pool=self.log)
        self.assertTrue(posted.issubset(api.messages))
        self.assertEqual(len([m for m in api.messages.values() if "issues 1/" in m["content"]]), 1)
        original = pool.atomic_json
        def lost(path, obj):
            original(path, obj)
            raise OSError("ack lost after replace")
        request = {"request_id": "retry-goal", "action": "task", "actor": "controller", "evidence": ["raw:goal"],
            "payload": {"id": "t1", "source": "approved_goal", "project": "project", "title": "goal",
                        "objective": "test", "scope": "local", "acceptance": "test", "approval_ref": "approved", "issue_ids": []}}
        with patch.object(pool, "atomic_json", side_effect=lost):
            with self.assertRaises(OSError):
                self.log.apply(self.home, request)
        self.log.apply(self.home, request)
        self.assertEqual(sum(e["request"]["request_id"] == "retry-goal" for e in self.log.events), 1)

    def test_hwb_window_binding_reuse_and_close_cannot_bypass_original_controller_ledger(self):
        self.goal()
        directory = self.home / "workers" / "worker-1"
        directory.mkdir(parents=True)
        meta = {"id": "worker-1", "tool": "codex", "status": "stopped", "pid": None,
                "created": "2026-10-24 00:00:00", "cwd": str(self.home)}
        (directory / "meta.json").write_text(json.dumps(meta))
        value = {"task_id": "t1", "kind": "hwb", "profile": "owner", "session_id": "worker-1",
                 "executor": "ops", "binding": review.binding(meta)}
        with self.assertRaises(ValueError):
            self.apply("window", dict(value, binding="f" * 64))
        self.apply("window", value)
        with self.assertRaises(ValueError):
            self.apply("window", dict(value, profile="other"))
        self.assertEqual(self.log.counts([{"id": "worker-1"}]), {"issues": 0, "tasks": 1, "windows": 1})
        wid = next(iter(self.log.data["windows"]))
        self.receipt(wid, "accepted")
        self.receipt(wid, "report")
        checks = {k: {"status": "passed", "evidence": ["raw:" + k]} for k in review.STAGES}
        self.apply("review", {"task_id": "t1", "approval_ref": "review-1", "checks": checks})
        close = {"task_id": "t1", "approval_ref": "close-1", "reason": "核验", "outcome": "completed"}
        with self.assertRaises(ValueError):
            self.apply("close", close)
        control = review.ReviewLog(self.home / "control.json")
        state = {"tasks": {"worker-1": {"thread_id": "10", "message_id": "11"}}}
        control.apply(self.home, "worker-1", "review", {"checks": checks}, controller="controller", approval_ref="hwb-review", board_state=state)
        control.apply(self.home, "worker-1", "close", {"outcome": "completed", "reason": "approved", "evidence": ["raw:hwb"]},
                      controller="controller", approval_ref="hwb-close", board_state=state)
        with self.assertRaisesRegex(ValueError, "HWB原始材料"):
            self.apply("close", close)
        self.apply("review", {"task_id": "t1", "approval_ref": "review-2", "checks": checks})
        self.apply("close", close)
        self.assertTrue(self.log.data["tasks"]["t1"]["closed"])

    def test_fresh_approval_required_for_new_review_after_reopen(self):
        self.goal()
        checks = {k: {"status": "not_applicable", "reason": "明确取消，未执行"} for k in review.STAGES}
        review_payload = {"task_id": "t1", "approval_ref": "review-first", "checks": checks}
        self.apply("review", review_payload)
        self.apply("close", {"task_id": "t1", "approval_ref": "close-first", "reason": "取消", "outcome": "cancelled"})
        self.apply("reopen", {"task_id": "t1", "approval_ref": "reopen-new", "reason": "重新开始"})
        with self.assertRaises(ValueError):
            self.apply("review", review_payload)

    def test_deleted_or_foreign_controlled_page_blocks_instead_of_recreating(self):
        self.discover()
        api = FakeDiscord()
        state = self.home / "state.json"
        board.Board(api, state).sync([], pool=self.log)
        saved = json.loads(state.read_text())
        mid = saved["messages"]["pool:issues:1"]["id"]
        del api.messages[mid]
        with self.assertRaises(RuntimeError):
            board.Board(api, state).sync([], pool=self.log)

    def test_multiple_native_cli_writers_serialize_without_lost_events(self):
        base = [sys.executable, str(Path(pool.__file__)), "--home", str(self.home), "--state-dir", str(self.home), "apply", "--file"]
        processes = []
        for i in range(4):
            incoming = self.home / f"run-{i}.json"
            incoming.write_text(json.dumps({"request_id": f"run-{i}", "action": "inspection", "actor": "ops", "evidence": ["raw:inspection"],
                "payload": {"source_ref": f"run-{i}", "project": "project", "observed_at": "2026-10-24T00:00:00Z", "summary": "无问题"}}))
            processes.append(subprocess.Popen(base + [str(incoming)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("head", stdout)
        self.assertEqual(len(pool.Pool(self.path).events), 4)

    def test_untrusted_payload_cannot_shadow_provenance_and_invalid_urls_are_rejected(self):
        with self.assertRaises(ValueError):
            self.apply("discover", {"source": "ops_inspection", "source_ref": "run", "project": "project",
                "fingerprint": "symptom", "title": "observation", "severity": "P2", "observed_at": "2026-10-24T00:00:00Z",
                "evidence": ["forged:shadow"]}, "ops")
        self.goal()
        for url in ("http://discord.com/channels/100/200", "https://discord.com@evil.invalid/channels/100/200", "https://discord.com/channels/100/200?token=hidden"):
            with self.assertRaises(ValueError):
                self.apply("window", {"task_id": "t1", "kind": "native", "profile": "ops", "session_id": "session",
                    "executor": "ops", "url": url})

    def test_close_rechecks_execution_sources_immediately_before_commit(self):
        # The original ledger tests exercise liveness; a race at the last source
        # read must also block the higher-level pool commit.
        self.goal()
        self.window()
        wid = next(iter(self.log.data["windows"]))
        self.receipt(wid, "accepted")
        self.receipt(wid, "report")
        checks = {k: {"status": "passed", "evidence": ["raw:" + k]} for k in review.STAGES}
        self.apply("review", {"task_id": "t1", "approval_ref": "review", "checks": checks})
        original = pool.Pool.preflight
        calls = []
        def changed(log, home, request):
            result = original(log, home, request)
            if request["action"] == "close":
                calls.append(1)
                if len(calls) > 1:
                    raise ValueError("执行材料在最终核验时变化")
            return result
        with patch.object(pool.Pool, "preflight", changed):
            with self.assertRaisesRegex(ValueError, "最终核验"):
                self.apply("close", {"task_id": "t1", "approval_ref": "close", "reason": "核验", "outcome": "completed"})
        self.assertFalse(pool.Pool(self.path).data["tasks"]["t1"]["closed"])

    def test_state_loss_recovers_retired_pages_and_duplicate_page_blocks_all_writes(self):
        self.goal()
        for i in range(6):
            self.window(session=f"session-{i}")
        for wid in self.log.data["windows"]:
            self.apply("receipt", {"task_id": "t1", "window_id": wid, "kind": "accepted",
                "progress": "L" * 300, "next_step": "N" * 300}, "ops")
        api = FakeDiscord()
        state = self.home / "state.json"
        board.Board(api, state).sync([task()], pool=self.log)
        for wid in self.log.data["windows"]:
            self.receipt(wid, "accepted")
        state.unlink()
        board.Board(api, state).sync([task()], pool=self.log)
        self.assertNotIn("LLLL", "\n".join(m["content"] for m in api.messages.values()))
        saved = json.loads(state.read_text())
        mid = saved["messages"]["pool:tasks:1"]["id"]
        duplicate = copy.deepcopy(api.messages[mid])
        duplicate["id"] = "99999"
        api.messages["99999"] = duplicate
        before = {m: api.operations[m] for m in ("POST", "PATCH", "PUT", "DELETE")}
        with self.assertRaises(RuntimeError):
            board.Board(api, state).sync([task("待验收")], pool=self.log)
        self.assertEqual(before, {m: api.operations[m] for m in before})
