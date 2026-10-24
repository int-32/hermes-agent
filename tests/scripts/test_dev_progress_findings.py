"""Round-one independent review counterexamples on the compatible candidate."""
import json
import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import dev_progress_pool as pool
import dev_progress_review as review
import dev_progress_cards as cards
import test_dev_progress_pool as fixtures
from dev_progress_evidence import versions
from test_dev_progress_board import FakeDiscord, board, task


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PoolTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def setup_task(self):
        self.f.goal()
        self.f.window()
        return next(iter(self.f.log.data["windows"]))

    def test_sec001_spoofed_worker_receipts_stay_unverified(self):
        wid = self.setup_task()
        self.f.guard.stop()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "different-worker"}):
            self.f.apply("receipt", {"task_id": "t1", "window_id": wid, "kind": "accepted", "progress": "冒充接单", "next_step": "继续"}, "ops")
            self.assertEqual(self.f.log.task_status("t1"), "ready")
            with self.assertRaises(PermissionError):
                self.f.apply("confirm_receipt", {"task_id": "t1", "claim_id": self.f.log.events[-1]["hash"], "approval_ref": "forged"})
        self.f.guard.start()

    def test_logic001_missing_pool_middle_tail_or_entire_kind_after_state_loss_refuses_all_writes(self):
        for missing in ("middle", "tail", "kind"):
            with self.subTest(missing=missing):
                for i in range(10):
                    self.f.discover(key=f"disk-{i}", run=f"run-{i}")
                api, state = FakeDiscord(), self.f.home / "state.json"
                board.Board(api, state).sync([], pool=self.f.log)
                saved = json.loads(state.read_text())
                keys = sorted(k for k in saved["messages"] if k.startswith("pool:issues:"))
                deleted = keys[1:2] if missing == "middle" else keys[-1:] if missing == "tail" else keys
                for key in deleted:
                    del api.messages[saved["messages"][key]["id"]]
                state.unlink()
                before = api.operations.copy()
                with self.assertRaises(RuntimeError):
                    board.Board(api, state).sync([], pool=self.f.log)
                for method in ("POST", "PATCH", "PUT", "DELETE"):
                    self.assertEqual(before[method], api.operations[method])

    def test_logic001_missing_overview_tail_after_state_loss_refuses_all_writes(self):
        api, state = FakeDiscord(), self.f.home / "state.json"
        rows = [dict(task(), id=f"task-{i}") for i in range(24)]
        board.Board(api, state).sync(rows)
        saved = json.loads(state.read_text())
        keys = sorted(k for k in saved["messages"] if k.startswith("overview:page:"))
        del api.messages[saved["messages"][keys[-1]]["id"]]
        state.unlink()
        before = api.operations.copy()
        with self.assertRaises(RuntimeError):
            board.Board(api, state).sync(rows)
        self.assertEqual(before["POST"], api.operations["POST"])

    def test_logic002_escalation_visible_and_late_or_lower_observations_cannot_erase_it(self):
        self.f.discover()
        payload = dict(self.f.log.events[-1]["request"]["payload"])
        payload.update(source_ref="run-escalated", severity="P0", title="严重恶化待核实", observed_at="2026-10-24T03:00:00Z")
        self.f.apply("discover", payload, "ops")
        iid = next(iter(self.f.log.data["issues"]))
        self.assertEqual(self.f.log.data["issues"][iid]["severity"], "P0")
        self.assertIn("高严重", json.dumps(cards.attention_fields([], {}, self.f.log, "100000000000000001"), ensure_ascii=False))
        for ref, observed in (("late", "2026-10-24T00:00:00Z"), ("lower", "2026-10-25T00:00:00Z")):
            self.f.apply("discover", dict(payload, source_ref=ref, severity="P3", observed_at=observed), "ops")
            self.assertEqual(self.f.log.data["issues"][iid]["latest_observation"], "run-escalated" if ref == "late" else "lower")
        issue = pool.Pool(self.f.path).data["issues"][iid]
        self.assertEqual(issue["severity"], "P0")
        self.assertEqual(issue["state"], "unverified")
        self.assertEqual(len(issue["observations"]), 4)

    def test_logic003_reopen_requires_explicit_issue_terminal_resolution(self):
        self.f.discover()
        iid = next(iter(self.f.log.data["issues"]))
        self.f.goal()
        self.f.apply("link", {"issue_id": iid, "task_id": "t1"})
        checks = {k: {"status": "not_applicable", "reason": "取消未执行"} for k in review.STAGES}
        self.f.apply("review", {"task_id": "t1", "approval_ref": "review", "checks": checks})
        self.f.apply("close", {"task_id": "t1", "approval_ref": "close", "reason": "取消", "outcome": "cancelled"})
        self.f.discover(key="duplicate-target")
        target = next(i for i in self.f.log.data["issues"] if i != iid)
        for state in ("closed", "deferred", "duplicate"):
            payload = {"issue_id": iid, "state": state, "reason": "总控处置"}
            if state == "duplicate":
                payload["duplicate_of"] = target
            self.f.apply("issue_state", payload)
            with self.assertRaises(ValueError):
                self.f.apply("reopen", {"task_id": "t1", "approval_ref": "new-reopen", "reason": "继续"})
        self.f.apply("issue_state", {"issue_id": iid, "state": "ready", "reason": "明确重开"})
        self.f.apply("reopen", {"task_id": "t1", "approval_ref": "new-reopen", "reason": "继续"})

    def test_logic004_mutable_material_changed_after_review_blocks_close(self):
        wid = self.setup_task()
        self.f.receipt(wid, "accepted")
        self.f.receipt(wid, "report")
        # A real local material reference must bind bytes, rather than its path.
        directory = self.f.home / "evidence"
        directory.mkdir(exist_ok=True)
        material = directory / "native.txt"
        material.write_text("version A", encoding="utf-8")
        payload = {"task_id": "t1", "window_id": wid, "kind": "report", "progress": "回传", "next_step": "验收"}
        event = self.f.log.apply(self.f.home, {"request_id": "material-report", "action": "receipt", "actor": "ops", "evidence": ["file:native.txt"], "payload": payload})
        self.f.apply("confirm_receipt", {"task_id": "t1", "claim_id": event["hash"], "approval_ref": "confirm-material"})
        checks = {k: {"status": "passed", "evidence": ["raw:" + k]} for k in review.STAGES}
        self.f.apply("review", {"task_id": "t1", "approval_ref": "review", "checks": checks})
        material.write_text("version B", encoding="utf-8")
        rendered = json.dumps(cards.pool_task_cards(self.f.log, {}, "100000000000000001", []), ensure_ascii=False)
        self.assertIn("已失效", rendered)
        with self.assertRaises(ValueError):
            self.f.apply("close", {"task_id": "t1", "approval_ref": "close", "reason": "核验", "outcome": "completed"})
        self.f.apply("review", {"task_id": "t1", "approval_ref": "review-B", "checks": checks})
        self.f.apply("close", {"task_id": "t1", "approval_ref": "close-B", "reason": "核验新版本", "outcome": "completed"})
        self.assertTrue(pool.Pool(self.f.path).data["tasks"]["t1"]["closed"])

    def test_sug001_changed_work_marks_old_acceptance_invalid(self):
        wid = self.setup_task()
        self.f.receipt(wid, "accepted")
        self.f.receipt(wid, "report")
        checks = {k: {"status": "passed", "evidence": ["raw:" + k]} for k in review.STAGES}
        self.f.apply("review", {"task_id": "t1", "approval_ref": "review", "checks": checks})
        self.f.receipt(wid, "report")
        rendered = json.dumps(cards.pool_task_cards(self.f.log, {}, "100000000000000001", []), ensure_ascii=False)
        self.assertIn("已失效", rendered)

    def test_sug002_invalid_snowflakes_rejected_without_network(self):
        for url in ("https://discord.com/channels/0/0", "https://discord.com/channels/" + "1" * 180 + "/2", "https://discord.com/channels/18446744073709551616/100000000000000001"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                pool.window_url(url)

    def test_unverified_claims_cannot_replace_confirmed_receipt_or_invalidate_review(self):
        wid = self.setup_task()
        self.f.receipt(wid, "accepted")
        self.f.receipt(wid, "report")
        before = copy.deepcopy(self.f.log.data["tasks"]["t1"]["receipts"])
        work = self.f.log.work_hash("t1")
        self.f.apply("receipt", {"task_id": "t1", "window_id": wid, "kind": "accepted", "progress": "未核实", "next_step": "未知", "blocker": "冒充阻塞"}, "ops")
        self.assertEqual(self.f.log.data["tasks"]["t1"]["receipts"], before)
        self.assertEqual(self.f.log.work_hash("t1"), work)
        self.assertEqual(pool.Pool(self.f.path).task_status("t1"), "awaiting_review")

    def test_native_material_reader_rejects_paths_symlinks_hardlinks_special_and_oversize(self):
        directory = self.f.home / "evidence"
        directory.mkdir()
        material = directory / "valid.txt"
        material.write_text("safe")
        self.assertEqual(len(versions(self.f.home, ["file:valid.txt"])["file:valid.txt"]), 64)
        (directory / "link.txt").symlink_to(material)
        (directory / "sub").symlink_to(directory, target_is_directory=True)
        os.link(material, directory / "hard.txt")
        (directory / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
        os.mkfifo(directory / "fifo.txt")
        for ref in ("artifact:path-only", "file:../pool.json", "file:/absolute.txt", "file:link.txt", "file:sub/valid.txt", "file:hard.txt", "file:large.txt", "file:fifo.txt", "file:.env"):
            with self.subTest(reference=ref), self.assertRaises((OSError, ValueError)):
                versions(self.f.home, [ref])

    def test_registered_links_are_not_described_as_verified_until_controller_confirms(self):
        wid = self.setup_task()
        rendered = json.dumps(cards.pool_task_cards(self.f.log, {}, "100000000000000001", []), ensure_ascii=False)
        self.assertIn("登记链接", rendered)
        self.assertNotIn("已核实执行窗口", rendered)
        self.f.receipt(wid, "accepted")
        rendered = json.dumps(cards.pool_task_cards(self.f.log, {}, "100000000000000001", []), ensure_ascii=False)
        self.assertIn("已核实执行窗口", rendered)

    def test_retired_high_severity_issue_observation_requires_new_disposition_without_reopen(self):
        self.f.discover()
        iid = next(iter(self.f.log.data["issues"]))
        self.f.apply("issue_state", {"issue_id": iid, "state": "closed", "reason": "已处置"})
        payload = dict(self.f.log.events[0]["request"]["payload"], severity="P1", source_ref="new-high")
        self.f.apply("discover", payload, "ops")
        self.assertEqual(self.f.log.issue_status(iid), "closed")
        self.assertEqual(cards.active_counts([], self.f.log)["issues"], 1)
        self.assertIn("高严重", json.dumps(cards.attention_fields([], {}, self.f.log, "100000000000000001"), ensure_ascii=False))
        self.f.apply("issue_state", {"issue_id": iid, "state": "deferred", "reason": "已独立重新核验"})
        self.assertEqual(cards.active_counts([], self.f.log)["issues"], 0)
        self.f.apply("discover", dict(payload, source_ref="another-high"), "ops")
        self.assertEqual(cards.active_counts([], self.f.log)["issues"], 1)

    def test_claim_confirmation_requires_versioned_native_material_and_cannot_be_reused_after_reopen(self):
        wid = self.setup_task()
        claim = self.f.apply("receipt", {"task_id": "t1", "window_id": wid, "kind": "accepted", "progress": "声明", "next_step": "核实"}, "ops")
        with self.assertRaises(ValueError):
            self.f.apply("confirm_receipt", {"task_id": "t1", "claim_id": claim["hash"], "approval_ref": "path-only"})
        self.assertEqual(pool.Pool(self.f.path).task_status("t1"), "ready")
        self.f.apply("confirm_receipt", {"task_id": "t1", "claim_id": claim["hash"], "approval_ref": "content-version"}, evidence=["sha256:" + "b" * 64])
        with self.assertRaises(ValueError):
            self.f.apply("confirm_receipt", {"task_id": "t1", "claim_id": claim["hash"], "approval_ref": "reused-claim"}, evidence=["sha256:" + "b" * 64])
        checks = {k: {"status": "not_applicable", "reason": "取消未执行"} for k in review.STAGES}
        self.f.apply("review", {"task_id": "t1", "approval_ref": "cancel-review", "checks": checks})
        self.f.apply("close", {"task_id": "t1", "approval_ref": "cancel-close", "reason": "取消", "outcome": "cancelled"})
        self.f.apply("reopen", {"task_id": "t1", "approval_ref": "explicit-reopen", "reason": "新一轮"})
        with self.assertRaises(KeyError):
            self.f.apply("confirm_receipt", {"task_id": "t1", "claim_id": claim["hash"], "approval_ref": "previous-round"}, evidence=["sha256:" + "b" * 64])
        self.assertEqual(pool.Pool(self.f.path).task_status("t1"), "ready")

    def test_publication_initial_post_and_commit_ack_loss_resume_without_replacing_ids(self):
        for stage in ("page", "commit"):
            with self.subTest(stage=stage):
                for i in range(12):
                    self.f.discover(key=f"initial-{i}", run=f"initial-{i}")
                api, state = FakeDiscord(), self.f.home / "publication-state.json"
                state.unlink(missing_ok=True)
                request = api.request

                def lose(method, path, body=None):
                    if method == "POST" and ((stage == "page" and body["content"].startswith("issues 1/")) or
                                              (stage == "commit" and body["content"].startswith("HWB发布清单：") and '"version":1' in body["content"])):
                        api.lose_reply = True
                    return request(method, path, body)

                with patch.object(api, "request", lose), self.assertRaises(RuntimeError):
                    board.Board(api, state).sync([], pool=self.f.log)
                ids = set(api.messages)
                state.unlink(missing_ok=True)
                board.Board(api, state).sync([], pool=self.f.log)
                self.assertTrue(ids.issubset(api.messages))
                before = api.operations.copy()
                state.unlink()
                board.Board(api, state).sync([], pool=self.f.log)
                self.assertEqual({m: api.operations[m] for m in ("POST", "PATCH", "PUT", "DELETE")},
                                 {m: before[m] for m in ("POST", "PATCH", "PUT", "DELETE")})

    def test_legacy_full_inventory_upgrades_and_legacy_missing_tail_refuses_before_writes(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                for i in range(12):
                    self.f.discover(key=f"legacy-{i}", run=f"legacy-{i}")
                api, state = FakeDiscord(), self.f.home / "legacy-state.json"
                state.unlink(missing_ok=True)
                board.Board(api, state).sync([], pool=self.f.log)
                saved = json.loads(state.read_text())
                for mid, message in list(api.messages.items()):
                    if message["content"].startswith("HWB发布清单："):
                        del api.messages[mid]
                    else:
                        message["content"] = message["content"].replace("\n发布协议：1", "")
                if missing:
                    keys = sorted(k for k in saved["messages"] if k.startswith("pool:issues:"))
                    del api.messages[saved["messages"][keys[-1]]["id"]]
                state.unlink()
                before = api.operations.copy()
                if missing:
                    with self.assertRaises(RuntimeError):
                        board.Board(api, state).sync([], pool=self.f.log)
                    self.assertEqual(before, api.operations)
                else:
                    ids = {k: v["id"] for k, v in saved["messages"].items() if k.startswith("pool:")}
                    board.Board(api, state).sync([], pool=self.f.log)
                    restored = json.loads(state.read_text())
                    self.assertEqual(ids, {k: restored["messages"][k]["id"] for k in ids})

    def test_committed_inventory_shrink_retains_history_and_missing_leaf_fails_closed(self):
        for i in range(30):
            self.f.discover(key=f"inventory-{i}", run=f"inventory-{i}")
        api, state = FakeDiscord(), self.f.home / "inventory-state.json"
        rows = [dict(task(), id=f"inventory-task-{i}") for i in range(20)]
        board.Board(api, state).sync(rows, pool=self.f.log)
        saved = json.loads(state.read_text())
        keys = {k: v["id"] for k, v in saved["messages"].items() if k.startswith("overview:page:")}
        board.Board(api, state).sync(rows[:1], pool=self.f.log)
        state.unlink()
        board.Board(api, state).sync(rows[:1], pool=self.f.log)
        self.assertTrue(all(mid in api.messages for mid in keys.values()))
        leaf = next(mid for mid, m in api.messages.items() if m["content"].startswith("HWB发布清单：") and '"entries":' in m["content"])
        del api.messages[leaf]
        state.unlink()
        before = api.operations.copy()
        with self.assertRaises(RuntimeError):
            board.Board(api, state).sync(rows[:1], pool=self.f.log)
        self.assertEqual(before, api.operations)

    def test_inventory_recovery_preserves_cached_channel_conflict_protection(self):
        for prefix in ("pool:issues:", "overview:page:"):
            with self.subTest(prefix=prefix):
                api, state = FakeDiscord(), self.f.home / "conflict-state.json"
                state.unlink(missing_ok=True)
                board.Board(api, state).sync([dict(task(), id=f"conflict-{i}") for i in range(20)], pool=self.f.log)
                saved = json.loads(state.read_text())
                key = next(k for k in saved["messages"] if k.startswith(prefix))
                saved["messages"][key]["channel"] = "wrong-channel"
                board.atomic_json(state, saved)
                before = api.operations.copy()
                with self.assertRaises(RuntimeError):
                    board.Board(api, state).sync([], pool=self.f.log)
                self.assertEqual(before, api.operations)

    def test_legacy_overview_directory_cannot_point_to_deleted_whole_page_set(self):
        api, state = FakeDiscord(), self.f.home / "legacy-overview-state.json"
        rows = [dict(task(), id=f"legacy-overview-{i}") for i in range(20)]
        board.Board(api, state).sync(rows)
        saved = json.loads(state.read_text())
        for mid, message in list(api.messages.items()):
            if message["content"].startswith("HWB发布清单："):
                del api.messages[mid]
        for key, row in saved["messages"].items():
            if key.startswith("overview:page:"):
                del api.messages[row["id"]]
        state.unlink()
        before = api.operations.copy()
        with self.assertRaises(RuntimeError):
            board.Board(api, state).sync(rows)
        self.assertEqual(before, api.operations)

    def test_publication_multilevel_inventory_round_trip_and_stable_fingerprint(self):
        import dev_progress_delivery as delivery

        api, state = FakeDiscord(), self.f.home / "large-inventory-state.json"
        publisher = board.Board(api, state)
        channel = delivery.ensure_window(publisher, "discovery")
        for n in range(1, 101):
            publisher.message(f"pool:index:{n}", channel, f"index {n}/100\n任务池记录：{self.f.log.head}")
        delivery.publish(publisher)
        roots = [m for m in api.messages.values() if m["content"].startswith(delivery.PREFIX) and '"version":1' in m["content"]]
        self.assertEqual(len(roots), 1)
        self.assertLessEqual(len(roots[0]["content"]), 2000)
        before = api.operations.copy()
        state.unlink()
        recovered = board.Board(api, state)
        delivery.validate(recovered)
        self.assertEqual(len([k for k in recovered.state["messages"] if k.startswith("pool:index:")]), 100)
        delivery.publish(recovered)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(api.operations[method], before[method])
