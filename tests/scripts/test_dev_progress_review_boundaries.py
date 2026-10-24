"""Final review: ordinary text, surviving navigation and orphan publication evidence."""
import copy
import json
import unittest

from test_dev_progress_board import FakeDiscord, board
import test_dev_progress_pool as fixtures
import dev_progress_cards as cards
import dev_progress_pool as pool
import dev_progress_review as review


class ReviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PoolTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.state = self.f.home / "state.json"
        self.api = FakeDiscord()

    def sync(self):
        board.Board(self.api, self.state).sync([], pool=self.f.log)
        return json.loads(self.state.read_text())

    def fresh(self):
        self.api = FakeDiscord()
        self.state.unlink(missing_ok=True)
        return self.sync()

    def remove_manifests(self, roots_only=False):
        for mid, message in list(self.api.messages.items()):
            if message["content"].startswith("HWB发布清单：") and (not roots_only or '"version":1' in message["content"]):
                del self.api.messages[mid]

    def assert_no_writes(self, before):
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(self.api.operations[method], before[method])

    def assert_refused(self):
        before, original = self.api.operations.copy(), copy.deepcopy(self.api.messages)
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assert_no_writes(before)
        self.assertEqual(original, self.api.messages)

    def test_r1_sec001_ordinary_manifest_words_without_marker_cannot_block_any_window(self):
        for location in ("parent", "discovery", "recovery"):
            for text in ("HWB发布清单：{}", "HWB发布清单：非JSON"):
                with self.subTest(location=location, text=text):
                    saved = self.fresh()
                    channel = self.api.channel_id if location == "parent" else saved["board_threads"][location]["id"]
                    mid = self.api.request("POST", f"/channels/{channel}/messages", {"content": text})["id"]
                    self.api.messages[mid]["author"] = {"id": "ordinary-member"}
                    before = self.api.operations.copy()
                    self.sync()
                    self.assert_no_writes(before)
                    self.assertEqual(self.api.messages[mid]["content"], text)

    def test_r1_sec001_unmarked_legacy_page_numbers_are_not_publication_evidence(self):
        saved = self.fresh()
        self.remove_manifests()
        channel = saved["board_threads"]["discovery"]["id"]
        self.api.request("POST", f"/channels/{channel}/messages", {"content": "issues 1/999\n任务池记录：" + self.f.log.head})
        self.state.unlink()
        restored = self.sync()
        for key, row in saved["messages"].items():
            if key.startswith("pool:"):
                self.assertEqual(restored["messages"][key]["id"], row["id"])

    def test_r1_logic001_lost_legacy_entry_and_entire_issue_kind_cannot_be_replaced(self):
        saved = self.fresh()
        self.remove_manifests()
        for key, row in saved["messages"].items():
            if key == "pool:entry" or key.startswith("pool:issues:"):
                del self.api.messages[row["id"]]
            elif key.startswith("pool:index:"):
                # Isolate the surviving overview's original entry link as evidence.
                self.api.messages[row["id"]].update(content=board.message_content(key, "index 1/1\n任务池记录：" + self.f.log.head), embeds=[])
        self.state.unlink()
        self.assert_refused()

    def test_r1_logic001_old_parent_layout_keeps_original_link_evidence_and_refuses(self):
        saved = self.fresh()
        self.remove_manifests()
        business = saved["board_threads"]["discovery"]["id"]
        for key, row in saved["messages"].items():
            if key.startswith("pool:"):
                self.api.messages[row["id"]]["channel_id"] = self.api.channel_id
        overview = self.api.messages[saved["messages"]["overview"]["id"]]
        serialized = json.dumps(overview).replace(f"/{business}/", f"/{self.api.channel_id}/")
        self.api.messages[overview["id"]] = json.loads(serialized)
        del self.api.messages[saved["messages"]["pool:entry"]["id"]]
        del self.api.messages[saved["messages"]["pool:issues:1"]["id"]]
        self.state.unlink()
        self.assert_refused()

    def test_r1_logic002_orphan_leaf_still_proves_missing_original_overview(self):
        saved = self.fresh()
        self.remove_manifests(roots_only=True)
        del self.api.messages[saved["messages"]["overview"]["id"]]
        self.state.unlink()
        self.assert_refused()

    def test_orphan_leaf_with_all_original_pages_can_finish_root_using_original_ids(self):
        saved = self.fresh()
        self.remove_manifests(roots_only=True)
        self.state.unlink()
        restored = self.sync()
        for key, row in saved["messages"].items():
            if key == "overview" or key.startswith("pool:"):
                self.assertEqual(restored["messages"][key]["id"], row["id"])

    def test_r1_sug001_hwb_material_and_control_changes_expire_display_acceptance(self):
        self.f.goal()
        directory = self.f.home / "workers" / "worker-1"
        directory.mkdir(parents=True)
        meta = {"id": "worker-1", "tool": "codex", "status": "stopped", "pid": None,
                "created": "2026-10-24 00:00:00", "cwd": str(self.f.home)}
        (directory / "meta.json").write_text(json.dumps(meta))
        self.f.apply("window", {"task_id": "t1", "kind": "hwb", "profile": "owner", "session_id": "worker-1", "executor": "ops", "binding": review.binding(meta)})
        checks = {k: {"status": "passed", "evidence": ["raw:" + k]} for k in review.STAGES}
        self.f.apply("review", {"task_id": "t1", "approval_ref": "pool-review", "checks": checks})
        self.assertTrue(self.f.log.review_valid("t1"))
        (directory / "events.jsonl").write_text('{"type":"message","text":"new source"}\n')
        self.assertFalse(self.f.log.review_valid("t1"))
        self.assertIn("已失效", json.dumps(cards.pool_task_cards(self.f.log, {}, self.api.guild_id, []), ensure_ascii=False))
        (directory / "events.jsonl").unlink()
        self.assertTrue(self.f.log.review_valid("t1"))
        control = review.ReviewLog(self.f.home / "control.json")
        control.apply(self.f.home, "worker-1", "review", {"checks": checks}, controller="controller", approval_ref="hwb-review",
                      board_state={"tasks": {"worker-1": {"thread_id": "10", "message_id": "11"}}})
        self.assertFalse(self.f.log.review_valid("t1"))

    def test_r1_sug002_new_journal_declares_receipt_semantics_and_old_receipts_fail_closed(self):
        self.f.goal()
        self.f.window()
        wid = next(iter(self.f.log.data["windows"]))
        self.f.receipt(wid, "accepted")
        obj = json.loads(self.f.path.read_text())
        self.assertEqual(obj["version"], 2)
        self.assertEqual(obj["receipt_semantics"], "claims-controller-confirmed-v1")
        old = {"version": 1, "events": obj["events"]}
        self.f.path.write_text(json.dumps(old))
        before = self.f.path.read_bytes()
        with self.assertRaises(ValueError):
            pool.Pool(self.f.path)
        self.assertEqual(self.f.path.read_bytes(), before)

    def test_legacy_without_receipts_preserves_every_event_hash_and_remote_anchor_on_upgrade(self):
        self.f.discover()
        events, head = copy.deepcopy(self.f.log.events), self.f.log.head
        self.f.path.write_text(json.dumps({"version": 1, "events": events}))
        self.f.log = pool.Pool(self.f.path)
        self.f.log.validate_anchor({"pool_head": head})
        self.assertEqual(self.f.log.events, events)
        self.f.discover(run="second")
        obj = json.loads(self.f.path.read_text())
        self.assertEqual(obj["events"][:-1], events)
        self.assertEqual(obj["events"][-1]["previous"], head)
        self.assertEqual(obj["version"], 2)

    def test_claimed_control_markers_still_enforce_author_target_and_protocol(self):
        for damage in ("manifest-author", "manifest-target", "manifest-format", "legacy-author", "legacy-target"):
            with self.subTest(damage=damage):
                saved = self.fresh()
                manifest = next(m for m in self.api.messages.values() if m["content"].startswith("HWB发布清单："))
                page = self.api.messages[saved["messages"]["pool:issues:1"]["id"]]
                mutations = {
                    "manifest-author": lambda: manifest.update(author={"id": "human"}),
                    "manifest-target": lambda: manifest.update(channel_id=self.api.channel_id),
                    "manifest-format": lambda: manifest.update(content="HWB发布清单：bad\n" + manifest["content"].splitlines()[-1]),
                    "legacy-author": lambda: page.update(author={"id": "human"}),
                    "legacy-target": lambda: page.update(channel_id=self.api.channel_id),
                }
                if damage.startswith("legacy"):
                    self.remove_manifests()
                mutations[damage]()
                self.state.unlink()
                self.assert_refused()

    def test_legacy_original_entry_links_in_fallback_and_index_remain_authoritative(self):
        for carrier in ("fallback", "index"):
            with self.subTest(carrier=carrier):
                saved = self.fresh()
                self.remove_manifests()
                overview = self.api.messages[saved["messages"]["overview"]["id"]]
                overview["embeds"] = []
                if carrier == "index":
                    overview["content"] = board.message_content("overview", "旧总览")
                else:
                    for key, row in saved["messages"].items():
                        if key.startswith("pool:index:"):
                            self.api.messages[row["id"]].update(content=board.message_content(key, "index 1/1\n任务池记录：" + self.f.log.head), embeds=[])
                del self.api.messages[saved["messages"]["pool:entry"]["id"]]
                del self.api.messages[saved["messages"]["pool:issues:1"]["id"]]
                self.state.unlink()
                self.assert_refused()

    def test_orphan_leaves_and_remaining_layout_guard_all_lost_page_directions(self):
        for i in range(12):
            self.f.discover(key=f"boundary-{i}", run=f"boundary-{i}")
        for loss in ("overview", "entry", "middle", "tail", "kind", "index", "recovery-thread"):
            with self.subTest(loss=loss):
                saved = self.fresh()
                self.remove_manifests(roots_only=True)
                issue_keys = sorted(k for k in saved["messages"] if k.startswith("pool:issues:"))
                missing = {"overview": ["overview"], "entry": ["pool:entry"], "middle": [issue_keys[1]],
                           "tail": [issue_keys[-1]], "kind": issue_keys + ["pool:entry"], "index": ["pool:index:1"], "recovery-thread": []}[loss]
                for key in missing:
                    del self.api.messages[saved["messages"][key]["id"]]
                if loss == "recovery-thread":
                    del self.api.threads[saved["board_threads"]["recovery"]["id"]]
                self.state.unlink()
                self.assert_refused()

    def test_orphan_intermediate_branch_cannot_ignore_missing_child_leaf(self):
        import dev_progress_delivery as delivery

        controller = board.Board(self.api, self.state)
        channel = delivery.ensure_window(controller, "discovery")
        for n in range(1, 101):
            controller.message(f"pool:index:{n}", channel, f"index {n}/100\n任务池记录：{self.f.log.head}")
        delivery.publish(controller)
        self.remove_manifests(roots_only=True)
        leaf = next(mid for mid, m in self.api.messages.items() if m["content"].startswith(delivery.PREFIX) and '"entries":' in m["content"])
        del self.api.messages[leaf]
        self.state.unlink()
        self.assert_refused()

    def test_unreferenced_trusted_leaf_must_not_override_channel_binding(self):
        import dev_progress_delivery as delivery

        saved = self.fresh()
        controller = board.Board(self.api, self.state)
        delivery.publish_node(controller, {"entries": [{"key": "overview", "id": saved["messages"]["overview"]["id"],
                                                        "channel": saved["board_threads"]["discovery"]["id"]}]})
        self.state.unlink()
        self.assert_refused()

    def test_explicit_semantics_missing_or_unknown_cannot_be_silently_accepted(self):
        self.f.goal()
        obj = json.loads(self.f.path.read_text())
        for value in (None, "direct-worker-receipt", "future-version"):
            with self.subTest(semantics=value):
                altered = dict(obj, receipt_semantics=value)
                self.f.path.write_text(json.dumps(altered))
                before = self.f.path.read_bytes()
                with self.assertRaises(ValueError):
                    pool.Pool(self.f.path)
                self.assertEqual(self.f.path.read_bytes(), before)

    def test_unversioned_confirmed_receipts_require_explicit_audited_migration(self):
        self.f.goal()
        self.f.window()
        self.f.receipt(next(iter(self.f.log.data["windows"])), "accepted")
        self.f.path.write_text(json.dumps({"version": 1, "events": self.f.log.events}))
        before = self.f.path.read_bytes()
        with self.assertRaises(ValueError):
            pool.Pool(self.f.path)
        self.assertEqual(self.f.path.read_bytes(), before)

    def test_ordinary_marker_examples_without_complete_token_cannot_claim_pool_authority(self):
        saved = self.fresh()
        channel = saved["board_threads"]["discovery"]["id"]
        self.api.request("POST", f"/channels/{channel}/messages", {"content": "任务池记录：文档示例 `[hwb-board:not-a-marker]`"})
        before = self.api.operations.copy()
        self.sync()
        self.assert_no_writes(before)

    def test_unmarked_comment_in_pending_window_does_not_prevent_acknowledged_registration(self):
        import dev_progress_delivery as delivery

        controller = board.Board(self.api, self.state)
        delivery.ensure_window(controller, "recovery")
        tid = self.api.thread(None, "问题发现", recover=False)
        controller.state["board_threads"]["discovery"] = {"id": tid, "pending": True}
        controller.save()
        self.api.request("POST", f"/channels/{tid}/messages", {"content": "普通文档示例 [hwb-board:placeholder]"})
        saved = self.sync()
        self.assertEqual(saved["board_threads"]["discovery"]["id"], tid)
