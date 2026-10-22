"""Controller acceptance is independent of worker lifecycle and rendering state."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(HERE))
try:
    import dev_progress_review as review
finally:
    sys.path.pop(0)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.directory = self.home / "workers" / "task-1"
        self.directory.mkdir(parents=True)
        self.meta = {"id": "task-1", "name": "历史任务", "tool": "codex", "cwd": str(self.home / "synthetic-project"),
                     "created": "2026-10-22 00:00:00", "status": "stopped", "pid": None}
        self.write_meta()
        self.log = review.ReviewLog(self.home / "control.json")
        self.acceptance = {key: {"status": "passed", "evidence": ["artifact:" + key], "reason": "总控已核验"}
                           for key in review.STAGES}
        self.board_state = {"tasks": {"task-1": {"thread_id": "10", "message_id": "11"}}}
        self.guard = patch.object(review, "require_controller")
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def write_meta(self):
        (self.directory / "meta.json").write_text(json.dumps(self.meta), encoding="utf-8")

    def apply(self, action, payload):
        return self.log.apply(self.home, "task-1", action, payload,
                              controller="总控测试", approval_ref="discord:explicit-test-approval",
                              board_state=self.board_state)

    def record(self):
        return self.apply("review", {"checks": self.acceptance})

    def close(self, outcome="completed"):
        return self.apply("close", {"outcome": outcome, "reason": "总控明确关单",
                                    "evidence": ["discord:close-evidence"]})

    def test_stopped_and_worker_claim_never_close_without_controller_record(self):
        self.meta["last_message"] = "全部完成并已上线"
        self.write_meta()
        tasks = review.project_tasks(review.scan_tasks(self.home), self.log)
        self.assertEqual(tasks[0]["status"], "待验收")
        self.assertFalse(tasks[0]["closed"])
        with self.assertRaises(ValueError):
            self.close()
        self.assertFalse(self.log.path.exists())

    def test_three_independent_checks_and_not_applicable_reason(self):
        self.acceptance["merge"] = {"status": "not_applicable", "evidence": [], "reason": "独立部署脚本，不合入业务主线"}
        self.acceptance["release"] = {"status": "not_applicable", "evidence": [], "reason": "本次为只读分析，无上线变更"}
        self.record()
        self.close()
        task = review.project_tasks(review.scan_tasks(self.home), self.log)[0]
        self.assertEqual(task["status"], "已完成")
        self.assertIn("上线验收：不适用", task["acceptance"])
        self.assertIn("无上线变更", task["acceptance"])

    def test_unverified_missing_evidence_and_reason_rejected(self):
        for key in review.STAGES:
            checks = json.loads(json.dumps(self.acceptance))
            checks[key]["evidence"] = []
            with self.assertRaises(ValueError):
                self.apply("review", {"checks": checks})
        self.acceptance["release"] = {"status": "not_applicable", "reason": "", "evidence": []}
        with self.assertRaises(ValueError):
            self.record()
        self.acceptance["release"] = {"status": "unverified", "reason": "待验收", "evidence": []}
        self.record()
        with self.assertRaises(ValueError):
            self.close()

    def test_pending_running_and_live_idle_worker_block_close(self):
        self.record()
        for status in ("running", "starting", "waiting_decision"):
            self.meta.update(status=status, pid=None)
            self.write_meta()
            with self.assertRaisesRegex(ValueError, "stop"):
                self.close()
        self.meta.update(status="idle", pid=123)
        self.write_meta()
        with patch.object(review, "alive", return_value=True):
            with self.assertRaisesRegex(ValueError, "stop"):
                self.close()
        self.meta.update(status="stopped", pid=None)
        self.write_meta()
        (self.directory / "decisions").mkdir()
        (self.directory / "decisions" / "1.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "pending"):
            self.close()

    def test_explicit_cancel_and_reopen_reset_acceptance_and_preserve_history(self):
        self.acceptance = {k: {"status": "not_applicable", "reason": "任务取消，无此项交付", "evidence": []}
                           for k in review.STAGES}
        self.record()
        self.close("cancelled")
        self.assertEqual(review.project_tasks(review.scan_tasks(self.home), self.log)[0]["status"], "已取消")
        self.apply("reopen", {"reason": "总控要求重新排查", "evidence": ["discord:reopen"]})
        task = review.project_tasks(review.scan_tasks(self.home), self.log)[0]
        self.assertEqual(task["status"], "待验收")
        self.assertFalse(task["closed"])
        self.assertIn("开发完成：未核验", task["acceptance"])
        self.assertTrue(task["reopen_event"])
        self.assertEqual(len(self.log.events), 3)
        with self.assertRaises(ValueError):
            self.close()

    def test_integrity_and_task_identity_mismatch_fail_closed(self):
        self.record()
        obj = json.loads(self.log.path.read_text(encoding="utf-8"))
        obj["events"][0]["controller"] = "被修改"
        self.log.path.write_text(json.dumps(obj), encoding="utf-8")
        with self.assertRaises(ValueError):
            review.ReviewLog(self.log.path)
        self.log.path.unlink()
        self.log = review.ReviewLog(self.log.path)
        self.record()
        self.meta["created"] = "2026-10-23 00:00:00"
        self.write_meta()
        with self.assertRaises(ValueError):
            self.close()

    def test_hash_chain_reload_and_evidence_references(self):
        self.record()
        self.close()
        loaded = review.ReviewLog(self.log.path)
        self.assertEqual(loaded.head, self.log.head)
        self.assertEqual(len(loaded.events), 2)
        self.assertEqual(loaded.current("task-1")["close"]["payload"]["evidence"], ["discord:close-evidence"])
        self.assertEqual(loaded.current("task-1")["review"]["payload"]["checks"], self.acceptance)

    def test_worker_guard_blocks_all_writes_including_historical_tasks(self):
        self.guard.stop()
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "managed-worker"}):
            with self.assertRaisesRegex(PermissionError, "worker"):
                self.record()
        self.assertFalse(self.log.path.exists())
        self.guard.start()

    def test_closed_worker_activity_conflict_never_silently_completes(self):
        self.record()
        self.close()
        self.meta.update(status="running", pid=123)
        self.write_meta()
        with patch.object(review, "alive", return_value=True):
            task = review.project_tasks(review.scan_tasks(self.home), self.log)[0]
        self.assertEqual(task["status"], "待决策")
        self.assertTrue(task["closed"])
        self.assertIn("关闭后", task["phase"])

    def test_candidates_are_read_only_and_reference_raw_evidence(self):
        (self.directory / "events.jsonl").write_text('{"kind":"turn_done","text":"完成"}\n', encoding="utf-8")
        candidates = review.candidates(self.home, self.log, self.board_state)
        self.assertEqual(candidates[0]["task_id"], "task-1")
        self.assertTrue(candidates[0]["close_blockers"])
        self.assertTrue(candidates[0]["raw_evidence"][0]["sha256"])
        self.assertFalse(self.log.path.exists())

    def test_rehashed_close_without_review_is_rejected(self):
        self.record()
        self.close()
        obj = json.loads(self.log.path.read_text(encoding="utf-8"))
        event = obj["events"][-1]
        event.update(sequence=1, previous=review.ZERO)
        event["hash"] = review.digest({k:v for k,v in event.items() if k!="hash"})
        self.log.path.write_text(json.dumps({"version":1,"events":[event]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            review.ReviewLog(self.log.path)

    def test_duplicate_record_is_idempotent_and_reopen_requires_fresh_approval(self):
        first = self.record()
        self.assertEqual(first["hash"],self.record()["hash"])
        self.assertEqual(len(self.log.events),1)
        self.close()
        event = self.apply("reopen", {"reason":"重新检查", "evidence":["discord:reopen"]})
        self.assertEqual(event["hash"],self.apply("reopen", {"reason":"重新检查", "evidence":["discord:reopen"]})["hash"])
        with self.assertRaises(ValueError):
            self.record()

    def test_registered_worker_ancestry_blocks_cleared_tool_environment(self):
        self.guard.stop()
        self.meta["pid"]=123
        self.write_meta()
        env={k:"" for k in ("CODEX_THREAD_ID","CLAUDECODE","CLAUDE_CODE_ENTRYPOINT","HERMES_KANBAN_TASK")}
        with patch.dict("os.environ",env),patch.object(review,"ancestry",return_value={123,456}):
            with self.assertRaises(PermissionError):
                self.record()
        self.assertFalse(self.log.path.exists())
        self.guard.start()

    def test_missing_control_log_with_delivered_head_fails_closed(self):
        self.record()
        self.close()
        state={"tasks":{"task-1":{"applied_control_head":self.log.head}}}
        review.validate_delivery_anchor(self.log,state)
        self.log.path.unlink()
        with self.assertRaises(ValueError):
            review.validate_delivery_anchor(review.ReviewLog(self.log.path),state)

    def test_record_ack_loss_is_idempotent_after_reload(self):
        original=review.atomic_json
        def lost(path,value):
            original(path,value)
            raise RuntimeError("local ack lost after replace")
        with patch.object(review,"atomic_json",side_effect=lost):
            with self.assertRaises(RuntimeError):
                self.record()
        self.record()
        self.assertEqual(len(self.log.events),1)

    def test_closed_task_survives_source_removal(self):
        self.record()
        self.close()
        (self.directory/"meta.json").unlink()
        projected=review.project_tasks([],self.log)
        self.assertEqual(projected[0]["status"],"已完成")
        self.assertEqual(projected[0]["hwb_status"],"源记录缺失")
        self.assertTrue(review.candidates(self.home,self.log,self.board_state))

    def test_changes_after_review_require_new_controller_review(self):
        self.record()
        self.meta["last_message"]="验收后又改了代码"
        self.write_meta()
        with self.assertRaisesRegex(ValueError,"验收后"):
            self.close()

    def test_worker_starting_during_close_is_rechecked_before_commit(self):
        self.record()
        original=review.scan_tasks
        def raced(home):
            tasks=original(home)
            self.meta.update(status="running",pid=None)
            self.write_meta()
            return tasks
        with patch.object(review,"scan_tasks",side_effect=raced):
            with self.assertRaises(ValueError):
                self.close()
        self.assertEqual(len(review.ReviewLog(self.log.path).events),1)


if __name__ == "__main__":
    unittest.main()
