import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import opennews_collector as collector


def item(item_id: int, second: int = 0) -> dict:
    return {
        "id": item_id,
        "engineType": "news",
        "newsType": "test",
        "text": f"item {item_id}",
        "ts": f"2026-07-10T10:00:{second:02d}+00:00",
    }


class CollectorBoundaryTests(unittest.TestCase):
    def run_collect(self, responses: dict[int, dict], *extra_args: str):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        args = collector.parse_args(
            [
                "--data-dir",
                str(data_dir),
                "--profile",
                "test",
                "--limit",
                "2",
                "--min-pages",
                "1",
                "--max-pages",
                "3",
                "--no-raw",
                *extra_args,
            ]
        )

        def fake_post_json(url, token, body, timeout):
            return responses[body["page"]]

        environment = {
            "OPENNEWS_TOKEN": "test-token",
            "OPENNEWS_DATA_DIR": str(data_dir),
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with mock.patch.object(collector, "post_json", side_effect=fake_post_json):
                summary = collector.collect(args)
        return data_dir, summary

    @staticmethod
    def write_seen_ids(data_dir: Path, ids: list[str]) -> None:
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "seen_ids.json").write_text(
            json.dumps({"ids": ids}),
            encoding="utf-8",
        )

    def test_intra_run_duplicate_does_not_confirm_history_boundary(self):
        responses = {
            1: {"data": [item(1), item(1)], "total": 3},
            2: {"data": [item(2, 1)], "total": 3},
            3: {"data": [], "total": 3},
        }

        data_dir, summary = self.run_collect(responses)

        self.assertEqual(summary["pages_fetched"], 3)
        self.assertEqual(summary["new_items"], 2)
        self.assertEqual(summary["intra_run_duplicates"], 1)
        self.assertEqual(summary["historical_items"], 0)
        self.assertFalse(summary["stopped_on_known_item"])
        self.assertTrue(summary["stopped_on_empty_page"])
        self.assertTrue(summary["boundary_confirmed"])
        self.assertEqual(summary["points_used"], 2)

        usage_files = list((data_dir / "usage").rglob("*.jsonl"))
        self.assertEqual(len(usage_files), 1)
        usage = json.loads(usage_files[0].read_text(encoding="utf-8"))
        self.assertEqual(usage["points_used"], 2)

    def test_explicit_stop_on_known_item_uses_pre_run_history_only(self):
        responses = {
            1: {"data": [item(2)], "total": 3},
            2: {"data": [item(1)], "total": 3},
            3: {"data": [item(3)], "total": 3},
        }
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        self.write_seen_ids(data_dir, ["1"])

        args = collector.parse_args(
            [
                "--profile",
                "test",
                "--limit",
                "2",
                "--max-pages",
                "3",
                "--no-raw",
                "--stop-on-known-item",
            ]
        )
        with mock.patch.dict(
            os.environ,
            {"OPENNEWS_TOKEN": "test-token", "OPENNEWS_DATA_DIR": str(data_dir)},
            clear=False,
        ):
            with mock.patch.object(
                collector,
                "post_json",
                side_effect=lambda url, token, body, timeout: responses[body["page"]],
            ):
                summary = collector.collect(args)

        self.assertEqual(summary["pages_fetched"], 2)
        self.assertEqual(summary["historical_items"], 1)
        self.assertTrue(summary["stopped_on_known_item"])
        self.assertTrue(summary["boundary_confirmed"])

    def test_default_mode_stops_on_fully_historical_page(self):
        responses = {
            1: {"data": [item(3)], "total": 3},
            2: {"data": [item(1), item(2)], "total": 3},
            3: {"data": [], "total": 3},
        }
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        self.write_seen_ids(data_dir, ["1", "2"])
        args = collector.parse_args(["--max-pages", "3", "--no-raw"])

        with mock.patch.dict(
            os.environ,
            {"OPENNEWS_TOKEN": "test-token", "OPENNEWS_DATA_DIR": str(data_dir)},
            clear=False,
        ):
            with mock.patch.object(
                collector,
                "post_json",
                side_effect=lambda url, token, body, timeout: responses[body["page"]],
            ):
                summary = collector.collect(args)

        self.assertEqual(summary["pages_fetched"], 2)
        self.assertEqual(summary["new_items"], 1)
        self.assertEqual(summary["historical_items"], 2)
        self.assertTrue(summary["stopped_on_known_page"])
        self.assertTrue(summary["boundary_confirmed"])

    def test_saturated_adaptive_run_uses_page_ceiling(self):
        args = collector.parse_args(
            [
                "--adaptive-pages",
                "--min-pages",
                "1",
                "--max-pages",
                "100",
            ]
        )

        self.assertEqual(collector.next_page_limit(args, 7, 7, False, False), 100)
        self.assertEqual(collector.next_page_limit(args, 7, 4, True, False), 5)

    def test_recovery_scan_can_start_after_page_one(self):
        responses = {
            101: {"data": [item(101)], "total": 10_000},
            102: {"data": [], "total": 10_000},
        }

        _, summary = self.run_collect(responses, "--start-page", "101")

        self.assertEqual(summary["start_page"], 101)
        self.assertEqual(summary["pages_fetched"], 2)
        self.assertEqual(summary["new_items"], 1)
        self.assertTrue(summary["stopped_on_empty_page"])

    def test_missing_requested_engine_is_reported(self):
        responses = {
            1: {"data": [item(1)], "total": 1},
            2: {"data": [], "total": 1},
        }

        _, summary = self.run_collect(
            responses,
            "--engine-type",
            "news",
            "--engine-type",
            "onchain",
        )

        self.assertEqual(summary["fetched_counts_by_engine"], {"news": 1})
        self.assertEqual(summary["missing_requested_engines"], ["onchain"])

    def test_min_score_is_sent_and_recorded(self):
        responses = {1: {"data": [], "total": 0}}
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        args = collector.parse_args(
            [
                "--data-dir",
                str(data_dir),
                "--profile",
                "score-50",
                "--limit",
                "2",
                "--min-score",
                "50",
                "--no-raw",
            ]
        )
        request_bodies: list[dict] = []

        def fake_post_json(url, token, body, timeout):
            request_bodies.append(body)
            return responses[body["page"]]

        with mock.patch.dict(
            os.environ,
            {"OPENNEWS_TOKEN": "test-token", "OPENNEWS_DATA_DIR": str(data_dir)},
            clear=False,
        ):
            with mock.patch.object(collector, "post_json", side_effect=fake_post_json):
                summary = collector.collect(args)

        self.assertEqual(request_bodies, [{"limit": 2, "page": 1, "score": 50}])
        self.assertEqual(summary["min_score"], 50)


if __name__ == "__main__":
    unittest.main()
