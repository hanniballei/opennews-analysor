import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from scripts import opennews_collector as collector
from scripts import opennews_enricher as enricher


def database_row(
    item_id: str,
    text: str,
    collected_at: str,
    *,
    title_source: str = "text",
    summary_zh: str | None = None,
) -> dict:
    return {
        "schema_version": 3,
        "id": item_id,
        "profile": "news",
        "engine_type": "news",
        "collected_at": collected_at,
        "event_date": collected_at[:10],
        "event_time": collected_at,
        "title": text,
        "title_source": title_source,
        "text": text,
        "summary_zh": summary_zh,
        "assets": [],
        "asset_details": [],
        "ai_status": "done",
        "raw": {"id": item_id, "engineType": "news", "text": text},
    }


class OpenNewsEnricherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.database_path = collector.database_path(self.data_dir, "news")

    def write_rows(self, rows: list[dict]) -> None:
        collector.write_rows_to_databases(self.data_dir, rows)

    def test_load_candidates_selects_long_or_multiline_text_in_collection_order(self):
        self.write_rows(
            [
                database_row("short", "Short headline", "2026-08-31T10:00:00Z"),
                database_row("long", "x" * 161, "2026-08-31T11:00:00Z"),
                database_row("multiline", "Line one\nLine two", "2026-08-31T12:00:00Z"),
                database_row(
                    "complete",
                    "y" * 200,
                    "2026-08-31T13:00:00Z",
                    title_source="deepseek",
                ),
                database_row("latest", "z" * 200, "2026-08-31T14:00:00Z"),
            ]
        )

        with sqlite3.connect(self.database_path) as connection:
            candidates = enricher.load_candidates(connection, min_text_length=160)

        self.assertEqual(
            [candidate["id"] for candidate in candidates],
            ["long", "multiline", "latest"],
        )

    def test_parse_enrichments_requires_exact_ids_and_bounded_fields(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "items": [
                                    {
                                        "id": "1",
                                        "title": "美国国债收益率升至阶段高点",
                                        "summary_zh": "油价上涨推高通胀和加息预期，美国长期国债收益率随之上升。",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        parsed = enricher.parse_enrichments(response, {"1"})

        self.assertEqual(parsed["1"]["title"], "美国国债收益率升至阶段高点")
        with self.assertRaisesRegex(RuntimeError, "IDs did not match"):
            enricher.parse_enrichments(response, {"1", "2"})

    def test_request_enrichments_retries_invalid_json_content(self):
        candidates = [{"id": "1", "text": "Long source text"}]
        invalid_response = {"choices": [{"message": {"content": ""}}]}
        valid_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "items": [
                                    {
                                        "id": "1",
                                        "title": "AI生成标题",
                                        "summary_zh": "AI生成摘要",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        with mock.patch.object(
            enricher,
            "post_json",
            side_effect=[invalid_response, valid_response],
        ) as post_json:
            result = enricher.request_enrichments(
                "https://api.deepseek.com/chat/completions",
                "test-key",
                "deepseek-v4-flash",
                candidates,
                timeout=60,
                max_tokens=32_768,
            )

        self.assertEqual(result["1"]["title"], "AI生成标题")
        self.assertEqual(post_json.call_count, 2)

    def test_request_enrichments_does_not_retry_nonrecoverable_api_error(self):
        candidates = [{"id": "1", "text": "Long source text"}]

        with mock.patch.object(
            enricher,
            "post_json",
            side_effect=RuntimeError("DeepSeek API returned HTTP 401"),
        ) as post_json:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                enricher.request_enrichments(
                    "https://api.deepseek.com/chat/completions",
                    "test-key",
                    "deepseek-v4-flash",
                    candidates,
                    timeout=60,
                    max_tokens=32_768,
                )

        self.assertEqual(post_json.call_count, 1)

    def test_truncated_batch_is_split_without_retrying_same_size(self):
        candidates = [
            {"id": str(item_id), "text": f"Long source text {item_id}"}
            for item_id in range(20)
        ]

        def fake_post_json(url, api_key, body, timeout):
            request_items = json.loads(body["messages"][1]["content"])["items"]
            if len(request_items) > 10:
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": ""},
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {
                                            "id": item["id"],
                                            "title": f"标题{item['id']}",
                                            "summary_zh": f"摘要{item['id']}",
                                        }
                                        for item in request_items
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ]
            }

        with mock.patch.object(enricher, "post_json", side_effect=fake_post_json) as post_json:
            batches = list(
                enricher.request_enrichment_batches(
                    "https://api.deepseek.com/chat/completions",
                    "test-key",
                    "deepseek-v4-flash",
                    candidates,
                    timeout=60,
                    max_tokens=32_768,
                )
            )

        self.assertEqual([len(batch) for batch, _ in batches], [10, 10])
        self.assertEqual(post_json.call_count, 3)

    def test_apply_enrichments_updates_display_fields_and_preserves_raw(self):
        original_summary = "上游已有摘要"
        self.write_rows(
            [
                database_row(
                    "1",
                    "Long source text " * 20,
                    "2026-08-31T10:00:00Z",
                    summary_zh=original_summary,
                ),
                database_row("2", "Another long source text " * 20, "2026-08-31T11:00:00Z"),
            ]
        )
        with sqlite3.connect(self.database_path) as connection:
            candidates = enricher.load_candidates(connection, min_text_length=160)
            raw_before = dict(
                connection.execute("SELECT id, raw_json FROM items ORDER BY id").fetchall()
            )
            updated = enricher.apply_enrichments(
                connection,
                candidates,
                {
                    "1": {"title": "第一条AI标题", "summary_zh": "第一条AI摘要"},
                    "2": {"title": "第二条AI标题", "summary_zh": "第二条AI摘要"},
                },
                now="2026-08-31T12:00:00Z",
            )
            stored = connection.execute(
                "SELECT id, title, title_source, text, summary_zh, payload_json, raw_json "
                "FROM items ORDER BY id"
            ).fetchall()

        self.assertEqual(updated, 2)
        self.assertEqual(stored[0][1:5], ("第一条AI标题", "deepseek", "Long source text " * 20, original_summary))
        self.assertEqual(stored[1][1:5], ("第二条AI标题", "deepseek", "Another long source text " * 20, "第二条AI摘要"))
        for item_id, title, title_source, _, summary_zh, payload_json, raw_json in stored:
            payload = json.loads(payload_json)
            self.assertEqual(payload["title"], title)
            self.assertEqual(payload["title_source"], title_source)
            self.assertEqual(payload["summary_zh"], summary_zh)
            self.assertEqual(raw_json, raw_before[item_id])

    def test_collector_refresh_preserves_deepseek_title_and_summary(self):
        original = database_row("1", "Original source text " * 20, "2026-08-31T10:00:00Z")
        self.write_rows([original])
        with sqlite3.connect(self.database_path) as connection:
            candidates = enricher.load_candidates(connection, min_text_length=160)
            enricher.apply_enrichments(
                connection,
                candidates,
                {"1": {"title": "AI生成标题", "summary_zh": "AI生成摘要"}},
                now="2026-08-31T11:00:00Z",
            )

        refreshed = {
            **original,
            "score": 95,
            "raw": {
                "id": "1",
                "engineType": "news",
                "text": original["text"],
                "score": 95,
            },
        }
        collector.write_rows_to_databases(self.data_dir, [refreshed])

        with sqlite3.connect(self.database_path) as connection:
            title, title_source, summary_zh, score, payload_json = connection.execute(
                "SELECT title, title_source, summary_zh, score, payload_json "
                "FROM items WHERE id = '1'"
            ).fetchone()
        payload = json.loads(payload_json)
        self.assertEqual((title, title_source, summary_zh, score), ("AI生成标题", "deepseek", "AI生成摘要", 95.0))
        self.assertEqual(payload["title"], "AI生成标题")
        self.assertEqual(payload["title_source"], "deepseek")
        self.assertEqual(payload["summary_zh"], "AI生成摘要")

    def test_apply_enrichments_uses_latest_payload_after_candidate_selection(self):
        original = database_row("1", "Original source text " * 20, "2026-08-31T10:00:00Z")
        self.write_rows([original])
        with sqlite3.connect(self.database_path) as connection:
            candidates = enricher.load_candidates(connection, min_text_length=160)

        collector.write_rows_to_databases(
            self.data_dir,
            [
                {
                    **original,
                    "score": 95,
                    "raw": {
                        "id": "1",
                        "engineType": "news",
                        "text": original["text"],
                        "score": 95,
                    },
                }
            ],
        )
        with sqlite3.connect(self.database_path) as connection:
            enricher.apply_enrichments(
                connection,
                candidates,
                {"1": {"title": "AI生成标题", "summary_zh": "AI生成摘要"}},
                now="2026-08-31T11:00:00Z",
            )
            score, payload_json = connection.execute(
                "SELECT score, payload_json FROM items WHERE id = '1'"
            ).fetchone()

        self.assertEqual(score, 95.0)
        self.assertEqual(json.loads(payload_json)["score"], 95)

    def test_existing_upstream_title_is_preserved_while_summary_is_generated(self):
        row = database_row(
            "1",
            "Long source text " * 20,
            "2026-08-31T10:00:00Z",
            title_source="title",
        )
        row["title"] = "Existing upstream title"
        self.write_rows([row])
        with sqlite3.connect(self.database_path) as connection:
            candidates = enricher.load_candidates(connection, min_text_length=160)
            updated = enricher.apply_enrichments(
                connection,
                candidates,
                {"1": {"title": "Unused AI title", "summary_zh": "AI生成摘要"}},
                now="2026-08-31T11:00:00Z",
            )
            stored = connection.execute(
                "SELECT title, title_source, summary_zh FROM items WHERE id = '1'"
            ).fetchone()

        self.assertEqual(updated, 1)
        self.assertEqual(stored, ("Existing upstream title", "title", "AI生成摘要"))

    def test_collector_refresh_preserves_generated_summary_for_upstream_title(self):
        row = database_row(
            "1",
            "Long source text " * 20,
            "2026-08-31T10:00:00Z",
            title_source="title",
        )
        row["title"] = "Existing upstream title"
        self.write_rows([row])
        with sqlite3.connect(self.database_path) as connection:
            candidates = enricher.load_candidates(connection, min_text_length=160)
            enricher.apply_enrichments(
                connection,
                candidates,
                {"1": {"title": "Unused AI title", "summary_zh": "AI生成摘要"}},
                now="2026-08-31T11:00:00Z",
            )

        collector.write_rows_to_databases(
            self.data_dir,
            [
                {
                    **row,
                    "score": 95,
                    "summary_zh": "上游新摘要",
                    "raw": {
                        "id": "1",
                        "engineType": "news",
                        "title": row["title"],
                        "text": row["text"],
                        "score": 95,
                    },
                }
            ],
        )

        with sqlite3.connect(self.database_path) as connection:
            stored = connection.execute(
                "SELECT title, title_source, summary_zh, score FROM items WHERE id = '1'"
            ).fetchone()
        self.assertEqual(
            stored,
            ("Existing upstream title", "title", "AI生成摘要", 95.0),
        )

    def test_noncanonical_state_timestamp_fails_fast(self):
        self.write_rows(
            [database_row("1", "Long source text " * 20, "2026-08-31T10:00:00Z")]
        )
        state_dir = self.data_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "news_enrichment.json").write_text(
            json.dumps({"through_collected_at": "2026-08-31 10:00:00"}),
            encoding="utf-8",
        )
        args = enricher.parse_args(["--data-dir", str(self.data_dir)])

        with mock.patch.dict(
            "os.environ",
            {"DEEPSEEK_API_KEY": "test-key", "OPENNEWS_DATA_DIR": str(self.data_dir)},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "UTC timestamp"):
                enricher.run(args)

    def test_chunk_candidates_honors_item_and_character_limits(self):
        candidates = [
            {"id": "1", "text": "a" * 90},
            {"id": "2", "text": "b" * 90},
            {"id": "3", "text": "c" * 10},
        ]

        chunks = list(
            enricher.chunk_candidates(candidates, batch_size=2, max_batch_characters=100)
        )

        self.assertEqual([[item["id"] for item in chunk] for chunk in chunks], [["1"], ["2", "3"]])

    def test_run_enriches_database_through_deepseek_response(self):
        self.write_rows(
            [database_row("1", "Long source text " * 20, "2026-08-31T10:00:00Z")]
        )
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "items": [
                                    {
                                        "id": "1",
                                        "title": "AI生成标题",
                                        "summary_zh": "AI生成摘要",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        args = enricher.parse_args(["--data-dir", str(self.data_dir)])

        with mock.patch.dict(
            "os.environ",
            {"DEEPSEEK_API_KEY": "test-key", "OPENNEWS_DATA_DIR": str(self.data_dir)},
            clear=False,
        ):
            with mock.patch.object(enricher, "post_json", return_value=response) as post_json:
                summary = enricher.run(args)

        self.assertEqual(summary["selected_items"], 1)
        self.assertEqual(summary["updated_items"], 1)
        self.assertEqual(summary["batches"], 1)
        request_body = post_json.call_args.args[2]
        self.assertEqual(request_body["model"], "deepseek-v4-flash")
        self.assertEqual(request_body["thinking"], {"type": "enabled"})
        self.assertEqual(request_body["reasoning_effort"], "high")
        self.assertEqual(request_body["max_tokens"], 32_768)
        self.assertEqual(request_body["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", request_body)
        with sqlite3.connect(self.database_path) as connection:
            stored = connection.execute(
                "SELECT title, title_source, summary_zh FROM items WHERE id = '1'"
            ).fetchone()
        self.assertEqual(stored, ("AI生成标题", "deepseek", "AI生成摘要"))
        state = json.loads(
            (self.data_dir / "state" / "news_enrichment.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["through_collected_at"], "2026-08-31T10:00:00Z")

    def test_run_processes_more_than_one_hundred_items_without_starvation(self):
        self.write_rows(
            [
                database_row(
                    str(item_id),
                    f"Long source text {item_id} " * 20,
                    "2026-08-31T10:00:00Z",
                )
                for item_id in range(101)
            ]
        )
        args = enricher.parse_args(["--data-dir", str(self.data_dir)])

        def fake_post_json(url, api_key, body, timeout):
            request_items = json.loads(body["messages"][1]["content"])["items"]
            response_items = [
                {
                    "id": request_item["id"],
                    "title": f"标题{request_item['id']}",
                    "summary_zh": f"摘要{request_item['id']}",
                }
                for request_item in request_items
            ]
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"items": response_items},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        with mock.patch.dict(
            "os.environ",
            {"DEEPSEEK_API_KEY": "test-key", "OPENNEWS_DATA_DIR": str(self.data_dir)},
            clear=False,
        ):
            with mock.patch.object(enricher, "post_json", side_effect=fake_post_json):
                summary = enricher.run(args)

        self.assertEqual(summary["selected_items"], 101)
        self.assertEqual(summary["updated_items"], 101)
        self.assertEqual(summary["batches"], 6)

    def test_invalid_numeric_configuration_fails_fast(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                enricher.parse_args(["--batch-size", "0"])


if __name__ == "__main__":
    unittest.main()
