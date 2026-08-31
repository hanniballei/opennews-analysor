import json
import os
import sqlite3
import subprocess
import sys
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
            91: {"data": [item(91)], "total": 10_000},
            92: {"data": [], "total": 10_000},
        }

        _, summary = self.run_collect(responses, "--start-page", "91")

        self.assertEqual(summary["start_page"], 91)
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
                "news-score-80",
                "--limit",
                "2",
                "--min-score",
                "80",
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

        self.assertEqual(request_bodies, [{"limit": 2, "page": 1, "score": 80}])
        self.assertEqual(summary["min_score"], 80)

    def test_normalized_row_preserves_message_provenance_and_asset_details(self):
        raw_item = {
            "id": 1,
            "engineType": "news",
            "newsType": "Twitter",
            "source": "example_account",
            "text": "Example <b>headline</b>",
            "description": "Example publisher",
            "link": "https://example.com/news/1",
            "ts": "2026-08-01T08:00:00.12+08:00",
            "score": 75,
            "coins": [
                {
                    "symbol": "BTC",
                    "market_type": "cex",
                    "score": 85,
                    "grade": "A",
                    "signal": "long",
                }
            ],
            "aiRating": {
                "status": "done",
                "score": 75,
                "grade": "A",
                "signal": "long",
                "summary": "",
                "enSummary": "",
            },
        }

        row = collector.normalize_item(
            raw_item,
            "1",
            collector.parse_event_time(raw_item, collector.utc_now()),
            "news",
        )

        self.assertEqual(row["schema_version"], 3)
        self.assertEqual(row["event_time"], "2026-08-01T00:00:00Z")
        self.assertEqual(row["event_time_source"], "ts")
        self.assertEqual(row["event_time_raw"], raw_item["ts"])
        self.assertIsNone(row["published_at"])
        self.assertEqual(row["source_raw"], "example_account")
        self.assertEqual(row["news_type"], "Twitter")
        self.assertEqual(row["description"], "Example publisher")
        self.assertEqual(row["title_source"], "text")
        self.assertEqual(row["assets"], ["BTC"])
        self.assertEqual(row["asset_details"], raw_item["coins"])
        self.assertEqual(row["ai_status"], "done")
        self.assertTrue(row["content_fingerprint"].startswith("sha256:"))

    def test_null_top_level_rating_fields_fall_back_to_ai_rating(self):
        raw_item = {
            "id": 1,
            "engineType": "news",
            "text": "Example",
            "ts": "2026-08-01T00:00:00Z",
            "score": None,
            "grade": None,
            "signal": None,
            "aiRating": {
                "status": "done",
                "score": 85,
                "grade": "A",
                "signal": "short",
            },
        }

        row = collector.normalize_item(raw_item, "1", collector.utc_now(), "news")

        self.assertEqual(row["score"], 85)
        self.assertEqual(row["grade"], "A")
        self.assertEqual(row["signal"], "short")

    def test_variable_precision_fractional_seconds_are_parsed(self):
        fallback = collector.utc_now()
        for fraction in ("1", "12", "1234", "12345", "123456"):
            with self.subTest(fraction=fraction):
                parsed = collector.parse_event_time(
                    {"ts": f"2026-08-01T08:00:00.{fraction}+08:00"},
                    fallback,
                )
                expected_microseconds = int(fraction[:6].ljust(6, "0"))
                self.assertEqual(parsed.microsecond, expected_microseconds)
                self.assertEqual(parsed.isoformat(), f"2026-08-01T00:00:00.{expected_microseconds:06d}+00:00")

    def test_pending_item_is_deferred_until_done_version_arrives(self):
        pending = item(1)
        pending["aiRating"] = {"status": "processing"}
        done = item(1, 1)
        done["aiRating"] = {
            "status": "done",
            "score": 75,
            "grade": "A",
            "signal": "long",
        }
        responses = {
            1: {"data": [pending], "total": 2},
            2: {"data": [done], "total": 2},
            3: {"data": [], "total": 2},
        }

        data_dir, summary = self.run_collect(responses)

        self.assertEqual(summary["deferred_items"], 1)
        self.assertEqual(summary["new_items"], 1)
        normalized_files = list((data_dir / "normalized").rglob("*.jsonl"))
        self.assertEqual(len(normalized_files), 1)
        row = json.loads(normalized_files[0].read_text(encoding="utf-8"))
        self.assertEqual(row["ai_status"], "done")

    def test_pending_listing_item_is_persisted_immediately(self):
        pending = item(1)
        pending["engineType"] = "listing"
        pending["aiRating"] = {"status": "processing"}
        responses = {1: {"data": [pending], "total": 1}}

        data_dir, summary = self.run_collect(
            responses,
            "--engine-type",
            "listing",
            "--split-profile-by-engine",
            "--max-pages",
            "1",
        )

        self.assertEqual(summary["deferred_items"], 0)
        self.assertEqual(summary["new_items"], 1)
        with sqlite3.connect(collector.database_path(data_dir, "listing")) as connection:
            stored = connection.execute(
                "SELECT id, ai_status FROM items"
            ).fetchall()
        self.assertEqual(stored, [("1", "processing")])

    def test_partition_append_is_idempotent_by_id(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        row = {"id": "1", "event_date": "2026-08-01"}

        first = collector.append_jsonl_by_date(data_dir, [row])
        second = collector.append_jsonl_by_date(data_dir, [row])

        self.assertEqual(first, [row])
        self.assertEqual(second, [])
        path = data_dir / "normalized" / "2026" / "08" / "01.jsonl"
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_seen_ids_are_unbounded_by_default(self):
        self.assertEqual(collector.parse_args([]).seen_retention, 0)

    def test_news_and_listing_use_independent_profiles(self):
        news_args = collector.parse_args(
            [
                "--profile",
                "news-score-80",
                "--engine-type",
                "news",
                "--split-profile-by-engine",
                "--min-score",
                "80",
            ]
        )
        listing_args = collector.parse_args(
            [
                "--profile",
                "listing-all",
                "--engine-type",
                "listing",
                "--split-profile-by-engine",
            ]
        )

        self.assertEqual(
            collector.build_engine_types(news_args.engine_type),
            {"news": []},
        )
        self.assertEqual(
            collector.build_engine_types(listing_args.engine_type),
            {"listing": []},
        )
        self.assertEqual(
            collector.profile_for_item({"engineType": "news"}, news_args),
            "news",
        )
        self.assertEqual(
            collector.profile_for_item({"engineType": "listing"}, listing_args),
            "listing",
        )
        self.assertEqual(news_args.min_score, 80)
        self.assertIsNone(listing_args.min_score)

    def test_listing_request_omits_score_filter(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        args = collector.parse_args(
            [
                "--data-dir",
                str(data_dir),
                "--profile",
                "listing-all",
                "--engine-type",
                "listing",
                "--split-profile-by-engine",
                "--limit",
                "100",
                "--min-pages",
                "1",
                "--max-pages",
                "1",
                "--no-raw",
            ]
        )
        request_bodies: list[dict] = []

        def fake_post_json(url, token, body, timeout):
            request_bodies.append(body)
            return {"data": [], "total": 0}

        with mock.patch.dict(
            os.environ,
            {"OPENNEWS_TOKEN": "test-token", "OPENNEWS_DATA_DIR": str(data_dir)},
            clear=False,
        ):
            with mock.patch.object(collector, "post_json", side_effect=fake_post_json):
                collector.collect(args)

        self.assertEqual(
            request_bodies,
            [
                {
                    "limit": 100,
                    "page": 1,
                    "engineTypes": {"listing": []},
                }
            ],
        )

    def test_adaptive_profile_can_start_with_deep_initial_scan(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        args = collector.parse_args(
            [
                "--adaptive-pages",
                "--min-pages",
                "1",
                "--initial-pages",
                "100",
                "--max-pages",
                "100",
            ]
        )

        self.assertEqual(
            collector.current_page_limit(args, Path(temp_dir.name) / "missing.json"),
            100,
        )

    def test_page_range_is_capped_at_api_page_100(self):
        args = collector.parse_args(
            ["--start-page", "91", "--max-pages", "410"]
        )

        self.assertEqual(args.start_page, 91)
        self.assertEqual(args.max_pages, 10)

    def test_database_writer_routes_news_and_listing_separately(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        common = {
            "schema_version": 3,
            "id": "same-id",
            "collected_at": "2026-08-01T00:00:00Z",
            "event_date": "2026-08-01",
            "event_time": "2026-08-01T00:00:00Z",
            "assets": [],
            "asset_details": [],
            "raw": {},
        }
        rows = [
            {**common, "profile": "news", "engine_type": "news", "title": "news"},
            {**common, "profile": "listing", "engine_type": "listing", "title": "listing"},
        ]

        result = collector.write_rows_to_databases(data_dir, rows)

        self.assertEqual(result["news"], {"inserted": 1, "updated": 0, "unchanged": 0})
        self.assertEqual(result["listing"], {"inserted": 1, "updated": 0, "unchanged": 0})
        for engine_type in ("news", "listing"):
            path = collector.database_path(data_dir, engine_type)
            with sqlite3.connect(path) as connection:
                stored = connection.execute(
                    "SELECT id, engine_type, title FROM items"
                ).fetchall()
            self.assertEqual(stored, [("same-id", engine_type, engine_type)])

    def test_database_writer_upgrades_more_complete_payload(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        incomplete = {
            "schema_version": 3,
            "id": "1",
            "profile": "news",
            "engine_type": "news",
            "collected_at": "2026-08-01T00:00:00Z",
            "event_date": "2026-08-01",
            "event_time": "2026-08-01T00:00:00Z",
            "title": "pending",
            "assets": [],
            "asset_details": [],
            "ai_status": "processing",
            "raw": {},
        }
        complete = {
            **incomplete,
            "title": "done",
            "score": 75,
            "signal": "long",
            "ai_status": "done",
        }

        first = collector.write_rows_to_databases(data_dir, [incomplete])
        second = collector.write_rows_to_databases(data_dir, [complete])

        self.assertEqual(first["news"]["inserted"], 1)
        self.assertEqual(second["news"]["updated"], 1)
        with sqlite3.connect(collector.database_path(data_dir, "news")) as connection:
            stored = connection.execute(
                "SELECT title, score, signal, ai_status FROM items WHERE id = '1'"
            ).fetchone()
        self.assertEqual(stored, ("done", 75.0, "long", "done"))

    def test_database_writer_accepts_changed_done_payload(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        original = {
            "schema_version": 3,
            "id": "1",
            "profile": "news",
            "engine_type": "news",
            "collected_at": "2026-08-01T00:00:00Z",
            "event_date": "2026-08-01",
            "event_time": "2026-08-01T00:00:00Z",
            "title": "original",
            "score": 80,
            "signal": "long",
            "assets": [],
            "asset_details": [],
            "ai_status": "done",
            "raw": {"score": 80, "signal": "long", "text": "original"},
        }
        corrected = {
            **original,
            "title": "corrected",
            "score": 90,
            "signal": "short",
            "raw": {"score": 90, "signal": "short", "text": "corrected"},
        }

        collector.write_rows_to_databases(data_dir, [original])
        result = collector.write_rows_to_databases(data_dir, [corrected])

        self.assertEqual(result["news"]["updated"], 1)
        with sqlite3.connect(collector.database_path(data_dir, "news")) as connection:
            stored = connection.execute(
                "SELECT title, score, signal FROM items WHERE id = '1'"
            ).fetchone()
        self.assertEqual(stored, ("corrected", 90.0, "short"))

    def test_newer_schema_overrides_legacy_quality_rank_scale(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        path = collector.database_path(data_dir, "news")
        collector.ensure_database(path)
        current = collector.database_record(
            {
                "schema_version": 3,
                "id": "1",
                "profile": "news",
                "engine_type": "news",
                "collected_at": "2026-08-01T00:00:00Z",
                "event_date": "2026-08-01",
                "title": "schema 3",
                "assets": [],
                "asset_details": [],
                "ai_status": "done",
                "raw": {"text": "schema 3"},
            },
            "2026-08-01T00:00:00Z",
        )
        legacy = {**current, "schema_version": 2, "quality_rank": 117, "title": "schema 2"}
        with sqlite3.connect(path) as connection:
            connection.execute(collector.DATABASE_UPSERT_SQL, legacy)

        result = collector.write_rows_to_databases(
            data_dir,
            [json.loads(current["payload_json"]) | {"raw": {"text": "schema 3"}}],
        )

        self.assertEqual(result["news"]["updated"], 1)
        with sqlite3.connect(path) as connection:
            stored = connection.execute(
                "SELECT schema_version, quality_rank, title FROM items WHERE id = '1'"
            ).fetchone()
        self.assertEqual(stored, (3, 3, "schema 3"))

    def test_historical_item_can_refresh_database_without_duplicate_archive_row(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        self.write_seen_ids(data_dir, ["news:1"])
        original = collector.normalize_item(
            {
                **item(1),
                "score": 80,
                "aiRating": {"status": "done", "score": 80, "signal": "long"},
            },
            "1",
            collector.utc_now(),
            "news",
        )
        collector.write_rows_to_databases(data_dir, [original])
        corrected = {
            **item(1),
            "text": "corrected",
            "score": 90,
            "aiRating": {"status": "done", "score": 90, "signal": "short"},
        }
        args = collector.parse_args(
            [
                "--data-dir",
                str(data_dir),
                "--profile",
                "news-score-80",
                "--engine-type",
                "news",
                "--split-profile-by-engine",
                "--min-score",
                "80",
                "--max-pages",
                "1",
                "--no-raw",
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
                return_value={"data": [corrected], "total": 1},
            ):
                summary = collector.collect(args)

        self.assertEqual(summary["new_items"], 0)
        self.assertEqual(summary["database_writes"]["news"]["updated"], 1)
        self.assertEqual(list((data_dir / "normalized").rglob("*.jsonl")), [])
        with sqlite3.connect(collector.database_path(data_dir, "news")) as connection:
            stored = connection.execute(
                "SELECT title, score, signal FROM items WHERE id = '1'"
            ).fetchone()
        self.assertEqual(stored, ("corrected", 90.0, "short"))

    def test_seen_keys_are_scoped_by_engine(self):
        historical = {"1", "listing:2", "news:3"}

        self.assertTrue(collector.is_historically_seen(historical, "news", "1"))
        self.assertFalse(collector.is_historically_seen(historical, "listing", "1"))
        self.assertTrue(collector.is_historically_seen(historical, "listing", "2"))
        self.assertTrue(collector.is_historically_seen(historical, "news", "3"))

    def test_legacy_row_is_normalized_for_database_migration(self):
        legacy = {
            "id": "1",
            "profile": "news",
            "engine_type": "news",
            "collected_at": "2026-08-01T01:00:00Z",
            "event_date": "2026-08-01",
            "title": "legacy",
            "assets": ["BTC"],
            "raw": {
                "id": 1,
                "engineType": "news",
                "newsType": "Reuters",
                "text": "legacy",
                "ts": "2026-08-01T08:00:00+08:00",
                "coins": [{"symbol": "BTC", "signal": "long", "score": 75}],
                "score": 75,
                "aiRating": {"status": "done", "signal": "long", "score": 75},
            },
        }

        migrated = collector.normalize_stored_row(legacy)

        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["engine_type"], "news")
        self.assertEqual(migrated["asset_details"], legacy["raw"]["coins"])
        self.assertEqual(migrated["event_time"], "2026-08-01T00:00:00Z")
        self.assertIsNone(
            collector.normalize_stored_row(
                {**legacy, "engine_type": "onchain", "raw": {"engineType": "onchain"}}
            )
        )

    def test_raw_migration_uses_payload_time_not_filename_order(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        data_dir = Path(temp_dir.name)
        raw_dir = data_dir / "raw" / "2026" / "08" / "01"
        raw_dir.mkdir(parents=True)

        def snapshot(collected_at: str, score: int, title: str) -> dict:
            return {
                "collected_at": collected_at,
                "pages": [
                    {
                        "data": [
                            {
                                "id": 1,
                                "engineType": "news",
                                "newsType": "test",
                                "text": title,
                                "ts": "2026-08-01T00:00:00Z",
                                "score": score,
                                "coins": [],
                                "aiRating": {
                                    "status": "done",
                                    "score": score,
                                    "signal": "long",
                                },
                            }
                        ]
                    }
                ],
            }

        (raw_dir / "opennews_z_early.json").write_text(
            json.dumps(snapshot("2026-08-01T01:00:00Z", 80, "early")),
            encoding="utf-8",
        )
        (raw_dir / "opennews_a_late.json").write_text(
            json.dumps(snapshot("2026-08-01T02:00:00Z", 90, "late")),
            encoding="utf-8",
        )
        script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_normalized_to_sqlite.py"

        subprocess.run(
            [sys.executable, str(script), "--data-dir", str(data_dir)],
            check=True,
            capture_output=True,
            text=True,
        )

        with sqlite3.connect(collector.database_path(data_dir, "news")) as connection:
            stored = connection.execute(
                "SELECT title, score FROM items WHERE id = '1'"
            ).fetchone()
        self.assertEqual(stored, ("late", 90.0))


if __name__ == "__main__":
    unittest.main()
