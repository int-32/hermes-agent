"""Verified review projection, rather than URL text, owns acceptance boundaries."""
import copy
import json
import unittest

from test_dev_progress_board import FakeDiscord, board, task
import test_dev_progress_review as fixtures
import dev_progress_cards as cards
import dev_progress_review as review


def acceptance_value(row):
    return next(f["value"] for f in cards.hwb_detail(row)["fields"] if f["name"] == "三层验收")


class AcceptancePartsTests(unittest.TestCase):
    def fixture(self, checks):
        f = fixtures.ReviewTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.acceptance = checks
        event = f.record()
        loaded = review.ReviewLog(f.log.path)
        row = review.project_tasks(review.scan_tasks(f.home), loaded)[0]
        return f, event, row

    def test_real_projection_preserves_url_stage_homographs_states_and_multiple_references(self):
        for word in ("记录", "未核验", "已核验", "不适用"):
            for status in ("passed", "unverified", "not_applicable"):
                with self.subTest(word=word, status=status):
                    urls = ["https://example.invalid/验收｜合入主线：" + word,
                            "https://example.invalid/｜上线验收：" + word]
                    checks = {"development": {"status": status, "evidence": urls, "reason": "待核实；" + urls[0]},
                              "merge": {"status": "unverified", "reason": "等总控读回"},
                              "release": {"status": "unverified", "reason": "尚未部署"}}
                    f, event, row = self.fixture(checks)
                    source = review.acceptance_text(event)
                    self.assertEqual(row["acceptance"], source)
                    for url in urls:
                        self.assertIn(url, source)  # The fixture must not truncate a reference.
                        self.assertIn(url, acceptance_value(row))
                    lines = acceptance_value(row).splitlines()
                    for prefix in ("开发完成：" + review.LABELS[status], "合入主线：未核验", "上线验收：未核验"):
                        self.assertEqual(sum(line.startswith(prefix) for line in lines), 1)
                    self.assertIn(source, cards.detail_content(row))
                    self.assertEqual(board.stage_snapshot(row)["acceptance"], source)
                    self.assertEqual(len(row["acceptance_parts"]), 3)
                    self.assertEqual("｜".join(row["acceptance_parts"]), source)
                    self.assertTrue(review.ReviewLog(f.log.path).events)

    def test_parts_are_transient_and_rebuilt_from_existing_validated_journal(self):
        checks = {key: {"status": "unverified", "reason": "待总控核实", "evidence": []} for key in review.STAGES}
        f, event, row = self.fixture(checks)
        before = f.log.path.read_bytes()
        sources = review.source_hashes(f.home, "task-1")
        again = review.project_tasks(review.scan_tasks(f.home), review.ReviewLog(f.log.path))[0]
        self.assertEqual(again["acceptance_parts"], row["acceptance_parts"])
        self.assertEqual("｜".join(row["acceptance_parts"]), review.acceptance_text(event))
        self.assertNotIn("acceptance_parts", json.dumps(json.loads(before)))
        self.assertNotIn("acceptance_parts", board.stage_snapshot(row))
        self.assertNotIn("acceptance_parts", cards.detail_content(row))
        self.assertEqual(review.source_hashes(f.home, "task-1"), sources)
        self.assertEqual(f.log.path.read_bytes(), before)
        state = f.home / "display-state.json"
        board.Board(FakeDiscord(), state).sync([row])
        self.assertNotIn("acceptance_parts", state.read_text())
        self.assertEqual(f.log.path.read_bytes(), before)
        empty = review.project_tasks([task()], review.ReviewLog(f.home / "empty.json"))[0]
        self.assertEqual(empty["acceptance_parts"], ["开发完成：未核验", "合入主线：未核验", "上线验收：未核验"])

    def test_unstructured_unknown_values_keep_old_single_line_without_guessing_url_boundaries(self):
        for source in (board.ACCEPTANCE, "未知验收｜合入主线：不应猜测",
                       "开发完成：证据https://example.invalid/｜合入主线：记录｜上线验收：未核验"):
            row = dict(task(), acceptance=source)
            self.assertEqual(acceptance_value(row), cards.clip(source, 900))
            self.assertIn(source, cards.detail_content(row))

    def test_mismatched_or_incomplete_parts_cannot_rebind_acceptance_facts(self):
        checks = {key: {"status": "unverified"} for key in review.STAGES}
        _, _, row = self.fixture(checks)
        for parts in (["开发完成：已核验", "合入主线：未核验", "上线验收：未核验"], ["仅一项"], "未知串"):
            with self.subTest(parts=parts), self.assertRaises(RuntimeError):
                acceptance_value(dict(row, acceptance_parts=parts))

    def test_real_closed_projection_accepts_only_exact_formats_without_rewriting_archive(self):
        checks = {key: {"status": "passed", "evidence": ["https://example.invalid/｜合入主线：记录"]} for key in review.STAGES}
        f, _, row = self.fixture(checks)
        api, state = FakeDiscord(), f.home / "state.json"
        board.Board(api, state).sync([row])
        saved = json.loads(state.read_text())
        f.board_state = saved
        f.close()
        closed = review.project_tasks(review.scan_tasks(f.home), review.ReviewLog(f.log.path))[0]
        board.Board(api, state).sync([closed])
        saved = json.loads(state.read_text())
        mid = saved["tasks"][closed["id"]]["message_id"]
        self.assertIn("https://example.invalid/｜合入主线：记录", acceptance_value(closed))
        for old in (False, True):
            if old:
                next(v for v in api.messages[mid]["embeds"][0]["fields"] if v["name"] == "三层验收")["value"] = cards.clip(closed["acceptance"], 900)
            before, original = api.operations.copy(), copy.deepcopy(api.messages)
            journal = f.log.path.read_bytes()
            state.unlink()
            row = review.project_tasks(review.scan_tasks(f.home), review.ReviewLog(f.log.path))[0]
            board.Board(api, state).sync([row])
            self.assertEqual(api.messages, original)
            self.assertEqual(f.log.path.read_bytes(), journal)
            for method in ("POST", "PATCH", "PUT", "DELETE"):
                self.assertEqual(api.operations[method], before[method])
        wrong = copy.deepcopy(api.messages[mid])
        value = next(v for v in wrong["embeds"][0]["fields"] if v["name"] == "三层验收")
        value["value"] = value["value"].replace("上线验收：已核验", "上线验收：未核验")
        with self.assertRaises(RuntimeError):
            cards.validate_closed_embed(closed, board.stage_snapshot(closed), wrong)
