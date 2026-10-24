"""Line layout must retain whole-text redaction, stage boundaries and page budgets."""
import copy
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from test_dev_progress_board import FakeDiscord, board, task
import test_dev_progress_pool as fixtures
import dev_progress_cards as cards
import dev_progress_review as review


def near_boundary_pool(fixture, issues=4):
    for n in range(issues):
        fixture.discover(key=f"budget-{n}", run=f"budget-{n}")
    ids = list(fixture.log.data["issues"])
    for iid in ids:
        fixture.apply("issue_state", {"issue_id": iid, "state": "ready", "reason": "synthetic observation verified"})
    checks = {key: {"status": "unverified", "reason": "r" * 150} for key in review.STAGES}
    with patch("dev_progress_pool.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime.fromisoformat("2026-10-24T12:34:56.123456+08:00")
        for letter in ("a", "b"):
            fixture.apply("task", {"id": letter * 100, "source": "approved_goal", "project": "p" * 60,
                "title": "t" * 100, "objective": "o" * 300, "scope": "s" * 300, "acceptance": "c" * 500,
                "approval_ref": "q" * 300, "issue_ids": ids})
            fixture.apply("review", {"task_id": letter * 100, "approval_ref": "review-" + letter, "checks": checks})
            assert fixture.log.review_valid(letter * 100)
    return fixture.log


class CardBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PoolTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_multiline_redacts_crossline_values_using_whole_text_semantics(self):
        for key in ("api_key", "api-key", "token", "password", "secret"):
            for separator in ("\n", "\r\n", "\n@ ", "\n` "):
                with self.subTest(key=key, separator=separator):
                    source = "说明\n" + key + ":" + separator + "EXAMPLE_VALUE\n下一步待核实"
                    self.assertNotIn("EXAMPLE_VALUE", cards.clip(source, 500))
                    rendered = cards.clip(source, 500, multiline=True)
                    self.assertNotIn("EXAMPLE_VALUE", rendered)
                    self.assertIn(key + "=（已隐藏）", rendered)
                    self.assertIn("下一步待核实", rendered)
                    self.assertLessEqual(cards.units(rendered), cards.units(cards.clip(source, 500)))

    def test_confirmed_receipt_display_redacts_but_keeps_original_synthetic_evidence(self):
        self.f.goal()
        self.f.window()
        wid = next(iter(self.f.log.data["windows"]))
        source = "api_key:\nEXAMPLE_VALUE"
        claim = self.f.apply("receipt", {"task_id": "t1", "window_id": wid, "kind": "accepted",
            "progress": source, "next_step": source, "blocker": source}, "ops")
        self.f.apply("confirm_receipt", {"task_id": "t1", "claim_id": claim["hash"], "approval_ref": "synthetic-readback"}, evidence=["sha256:" + "a" * 64])
        before = self.f.path.read_bytes()
        rendered = json.dumps(cards.pool_task_cards(self.f.log, {}, "100000000000000001", []), ensure_ascii=False)
        self.assertNotIn("EXAMPLE_VALUE", rendered)
        self.assertIn("已隐藏", rendered)
        self.assertEqual(self.f.log.data["tasks"]["t1"]["receipts"][wid]["progress"], source)
        self.assertEqual(self.f.path.read_bytes(), before)

    def test_real_acceptance_text_splits_system_stages_before_evidence_url(self):
        url = "https://example.invalid/evidence?a=1;b=2"
        checks = {"development": {"status": "passed", "evidence": [url]},
                  "merge": {"status": "unverified"}, "release": {"status": "unverified"}}
        source = review.acceptance_text({"payload": {"checks": checks}})
        row = dict(task(), acceptance=source)
        fields = cards.hwb_detail(row)["fields"]
        rendered = next(f["value"] for f in fields if f["name"] == "三层验收")
        for stage in ("开发完成：已核验", "合入主线：未核验", "上线验收：未核验"):
            self.assertTrue(any(line == stage or line.startswith(stage + "；") for line in rendered.splitlines()), stage)
        url_line = next(line for line in rendered.splitlines() if url in line)
        self.assertNotIn("合入主线", url_line)
        self.assertNotIn("上线验收", url_line)
        self.assertLessEqual(cards.units(rendered), cards.units(cards.clip(source, 900)))
        self.assertIn(source, cards.detail_content(row))

    def test_near_4800_valid_reviewed_cards_keep_original_single_page_budget(self):
        log = near_boundary_pool(self.f)
        rendered = cards.pool_task_cards(log, {}, "100000000000000001", [])
        self.assertEqual(len(rendered), 2)
        # Four legal associations put the original cards at 4770 total units;
        # the extra labels in 2d0b grow that same pair to 4808 and add a page.
        for embed in rendered:
            self.assertLessEqual(cards.embed_units(embed), 2385)
            self.assertEqual(len(embed["fields"]), 12)
        self.assertLessEqual(sum(cards.embed_units(e) for e in rendered), 4770)
        self.assertEqual(len(cards.card_pages(rendered)), 1)
        api, state = FakeDiscord(), self.f.home / "state.json"
        board.Board(api, state).sync([], pool=log)
        saved = json.loads(state.read_text())
        self.assertEqual(saved["pool_pages"]["tasks"], 1)
        page = api.messages[saved["messages"]["pool:tasks:1"]["id"]]
        self.assertTrue(page["content"].startswith("tasks 1/1\n"))
        self.assertEqual(len(page["embeds"]), 2)
        before, original = api.operations.copy(), copy.deepcopy(api.messages)
        state.unlink()
        board.Board(api, state).sync([], pool=log)
        self.assertEqual(api.messages, original)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(api.operations[method], before[method])

    def test_literal_single_issue_review_fixture_is_not_itself_over_the_page_budget(self):
        rendered = cards.pool_task_cards(near_boundary_pool(self.f, issues=1), {}, "100000000000000001", [])
        self.assertEqual(len(rendered), 2)
        self.assertTrue(all(len(e["fields"]) == 9 for e in rendered))
        self.assertLessEqual(sum(cards.embed_units(e) for e in rendered), 4628)
        self.assertEqual(len(cards.card_pages(rendered)), 1)

    def test_multiline_budget_never_exceeds_same_original_single_line_value(self):
        for source in ("已有\n换行；开发未完成。上线待核验", "😀；" * 400, "😀" * 600, "a" + "😀" * 600,
                       "阶段一｜阶段二｜阶段三", "api_key:\nEXAMPLE_VALUE\n继续核实",
                       "x" * 600 + "。" + "y" * 600):
            for limit in (10, 40, 150, 300, 500, 900, 1024):
                with self.subTest(limit=limit, source=source[:10]):
                    self.assertEqual(cards.units(cards.clip(source, limit, multiline=True)), cards.units(cards.clip(source, limit)))

    def test_closed_url_acceptance_keeps_exact_old_or_new_format_and_unique_fields(self):
        checks = {"development": {"status": "passed", "evidence": ["https://example.invalid/evidence"]},
                  "merge": {"status": "unverified"}, "release": {"status": "unverified"}}
        source = review.acceptance_text({"payload": {"checks": checks}})
        row = dict(task("已完成"), acceptance=source, control_event={"approval_ref": "synthetic-close"})
        detail = {"embeds": [cards.hwb_detail(row)]}
        latest = {"status": row["status"], "acceptance": source}
        cards.validate_closed_embed(row, latest, detail)
        old = copy.deepcopy(detail)
        next(f for f in old["embeds"][0]["fields"] if f["name"] == "三层验收")["value"] = cards.clip(source, 900)
        cards.validate_closed_embed(row, latest, old)
        wrong = copy.deepcopy(detail)
        value = next(f for f in wrong["embeds"][0]["fields"] if f["name"] == "三层验收")
        value["value"] = value["value"].replace("上线验收：未核验", "上线验收：已核验")
        with self.assertRaises(RuntimeError):
            cards.validate_closed_embed(row, latest, wrong)
        detail["embeds"][0]["fields"].append(copy.deepcopy(next(f for f in detail["embeds"][0]["fields"] if f["name"] == "三层验收")))
        with self.assertRaises(RuntimeError):
            cards.validate_closed_embed(row, latest, detail)
