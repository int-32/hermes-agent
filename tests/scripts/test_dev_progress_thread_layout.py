"""Parent stays a single overview; public business and recovery windows own detail."""
import copy
import json
import unittest
from unittest.mock import patch

from test_dev_progress_board import FakeDiscord, board, task
import test_dev_progress_pool as fixtures


class ThreadLayoutTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.api = FakeDiscord()
        self.state = self.fixture.home / "state.json"
        self.rows = [dict(task(), id=f"任务-{n}", name=f"中文任务-{n}") for n in range(24)]

    def sync(self):
        board.Board(self.api, self.state).sync(self.rows, pool=self.fixture.log)
        return json.loads(self.state.read_text())

    def assert_writes_equal(self, before):
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(self.api.operations[method], before[method])

    def test_existing_parent_overview_is_only_parent_message_and_links_public_window(self):
        original = board.Board(self.api, self.state).message("overview", self.api.channel_id, "原总览")
        start = len(self.api.calls)
        saved = self.sync()
        self.assertEqual([m["id"] for m in self.api.history(self.api.channel_id)], [original])
        business = saved["board_threads"]["discovery"]["id"]
        recovery = saved["board_threads"]["recovery"]["id"]
        self.assertNotEqual(business, recovery)
        for key, row in saved["messages"].items():
            if key.startswith(("pool:", "overview:page:")):
                self.assertEqual(row["channel"], business)
            if key.startswith("delivery:"):
                self.assertEqual(row["channel"], recovery)
        overview = self.api.messages[original]
        self.assertIn(f"/{business}/", json.dumps(overview, ensure_ascii=False))
        self.assertNotIn("HWB发布清单：", json.dumps(self.api.history(business), ensure_ascii=False))
        for method, path, _ in self.api.calls[start:]:
            if method in {"POST", "PATCH", "DELETE"} and path.startswith(f"/channels/{self.api.channel_id}/messages"):
                self.assertEqual((method, path), ("PATCH", f"/channels/{self.api.channel_id}/messages/{original}"))

    def test_state_loss_recovers_marked_renamed_threads_and_original_ids_with_zero_writes(self):
        saved = self.sync()
        self.api.threads[saved["board_threads"]["discovery"]["id"]] = "已改显示名"
        self.api.threads[saved["board_threads"]["recovery"]["id"]] = "已改技术名"
        ids, before = set(self.api.messages), self.api.operations.copy()
        self.state.unlink()
        restored = self.sync()
        self.assertEqual(restored["board_threads"], saved["board_threads"])
        self.assertEqual(set(self.api.messages), ids)
        self.assert_writes_equal(before)

    def test_page_post_ack_loss_resumes_in_same_threads_without_parent_spill(self):
        request = self.api.request

        def lose(method, path, body=None):
            if method == "POST" and body and body.get("content", "").startswith("issues 1/"):
                self.api.lose_reply = True
            return request(method, path, body)

        with patch.object(self.api, "request", lose), self.assertRaises(RuntimeError):
            self.sync()
        ids, threads = set(self.api.messages), copy.deepcopy(self.api.threads)
        self.state.unlink(missing_ok=True)
        saved = self.sync()
        self.assertTrue(ids.issubset(self.api.messages))
        self.assertTrue(all(self.api.threads[tid] == name for tid, name in threads.items()))
        self.assertEqual([m["id"] for m in self.api.history(self.api.channel_id)], [saved["messages"]["overview"]["id"]])
        before = self.api.operations.copy()
        self.sync()
        self.assert_writes_equal(before)

    def test_same_name_without_marker_is_not_an_identity_or_permission_to_create_again(self):
        self.api.thread(None, "问题发现")
        before, threads = self.api.operations.copy(), dict(self.api.threads)
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assertEqual(self.api.threads, threads)
        self.assert_writes_equal(before)

    def duplicate_registration(self, mid):
        other = self.api.thread(None, "任意显示名")
        self.api.messages["duplicate-id"] = dict(self.api.messages[mid], id="duplicate-id", channel_id=other)

    def test_missing_wrong_or_duplicate_registration_stops_before_writes_even_without_state(self):
        for damage in ("missing-thread", "missing-marker", "wrong-parent", "wrong-guild", "wrong-owner", "private", "duplicate", "marker-author", "marker-content"):
            with self.subTest(damage=damage):
                self.api = FakeDiscord()
                self.state.unlink(missing_ok=True)
                saved = self.sync()
                row = saved["board_threads"]["discovery"]
                tid, mid = row["id"], row["marker_id"]
                mutations = {
                    "missing-thread": lambda: self.api.threads.pop(tid),
                    "missing-marker": lambda: self.api.messages.pop(mid),
                    "wrong-parent": lambda: self.api.thread_meta.update({tid: {"parent_id": "foreign"}}),
                    "wrong-guild": lambda: self.api.thread_meta.update({tid: {"guild_id": "foreign"}}),
                    "wrong-owner": lambda: self.api.thread_meta.update({tid: {"owner_id": "human"}}),
                    "private": lambda: self.api.thread_meta.update({tid: {"type": 12}}),
                    "duplicate": lambda: self.duplicate_registration(mid),
                    "marker-author": lambda: self.api.messages[mid].update(author={"id": "human"}),
                    "marker-content": lambda: self.api.messages[mid].update(content="损坏" + self.api.messages[mid]["content"]),
                }
                mutations[damage]()
                self.state.unlink()
                before, messages, threads = self.api.operations.copy(), copy.deepcopy(self.api.messages), dict(self.api.threads)
                with self.assertRaises(RuntimeError):
                    self.sync()
                self.assertEqual(self.api.messages, messages)
                self.assertEqual(self.api.threads, threads)
                self.assert_writes_equal(before)

    def test_first_marker_ack_loss_recovers_original_thread_and_marker(self):
        request = self.api.request

        def lose(method, path, body=None):
            if method == "POST" and body and body.get("content", "").startswith("看板恢复记录\n"):
                self.api.lose_reply = True
            return request(method, path, body)

        with patch.object(self.api, "request", lose), self.assertRaises(RuntimeError):
            self.sync()
        ids, threads = set(self.api.messages), dict(self.api.threads)
        self.state.unlink()
        saved = self.sync()
        self.assertTrue(ids.issubset(self.api.messages))
        self.assertEqual(saved["board_threads"]["recovery"]["id"], next(tid for tid, name in threads.items() if name == "看板恢复记录"))

    def test_unacknowledged_thread_creation_stops_instead_of_adopting_by_name(self):
        thread = self.api.thread

        def lose(tid, name, *args, **kwargs):
            result = thread(tid, name, *args, **kwargs)
            if tid is None and name == "看板恢复记录":
                raise RuntimeError("thread creation response lost")
            return result

        with patch.object(self.api, "thread", lose), self.assertRaises(RuntimeError):
            self.sync()
        before, threads = self.api.operations.copy(), dict(self.api.threads)
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assertEqual(self.api.threads, threads)
        self.assert_writes_equal(before)

    def test_archived_windows_stay_read_only_on_unchanged_poll_and_reopen_only_for_new_delivery(self):
        saved = self.sync()
        window_ids = {row["id"] for row in saved["board_threads"].values()}
        self.api.archived.update(window_ids)
        before = self.api.operations.copy()
        self.state.unlink()
        self.sync()
        self.assert_writes_equal(before)
        self.assertTrue(window_ids.issubset(self.api.archived))
        self.fixture.discover()
        self.sync()
        self.assertNotIn(saved["board_threads"]["discovery"]["id"], self.api.archived)

    def test_recovery_json_misrouted_into_parent_is_rejected_without_replacement(self):
        self.sync()
        manifest = next(m for m in self.api.messages.values() if m["content"].startswith("HWB发布清单："))
        manifest["channel_id"] = self.api.channel_id
        self.state.unlink()
        before = self.api.operations.copy()
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assert_writes_equal(before)

    def test_damaged_pending_cache_cannot_replace_a_published_window_marker(self):
        saved = self.sync()
        row = saved["board_threads"]["discovery"]
        del self.api.messages[row.pop("marker_id")]
        row["pending"] = True
        board.atomic_json(self.state, saved)
        before = self.api.operations.copy()
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assert_writes_equal(before)

    def test_cross_role_display_name_cannot_redirect_new_window_creation(self):
        import dev_progress_delivery as delivery

        controller = board.Board(self.api, self.state)
        recovery = delivery.ensure_window(controller, "recovery")
        self.api.threads[recovery] = "问题发现"
        saved = self.sync()
        self.assertEqual(saved["board_threads"]["recovery"]["id"], recovery)
        self.assertNotEqual(saved["board_threads"]["discovery"]["id"], recovery)
