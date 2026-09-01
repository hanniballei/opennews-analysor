import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import opennews_collector as collector
from scripts import opennews_listing_enricher as listing_enricher


def listing_row(
    item_id: str,
    text: str,
    collected_at: str,
    *,
    exchange: str = "bybit",
    assets: list[str] | None = None,
    link: str | None = None,
) -> dict:
    assets = assets or []
    return {
        "schema_version": 3,
        "id": item_id,
        "profile": "listing",
        "engine_type": "listing",
        "collected_at": collected_at,
        "event_date": collected_at[:10],
        "event_time": collected_at,
        "event_time_source": "ts",
        "event_time_raw": collected_at,
        "source": exchange,
        "source_raw": exchange,
        "news_type": exchange,
        "title": text,
        "title_source": "text",
        "text": text,
        "link": link,
        "assets": assets,
        "asset_details": [
            {"symbol": symbol, "market_type": "cex"} for symbol in assets
        ],
        "score": 80,
        "grade": "A",
        "signal": "long",
        "ai_status": "done",
        "raw": {
            "id": item_id,
            "engineType": "listing",
            "source": exchange,
            "newsType": exchange,
            "text": text,
            "link": link,
            "coins": [
                {"symbol": symbol, "market_type": "cex"} for symbol in assets
            ],
        },
    }


def classification_response(items: list[dict]) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({"items": items}, ensure_ascii=False)
                },
            }
        ]
    }


class OpenNewsListingEnricherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.database_path = collector.database_path(self.data_dir, "listing")

    def write_rows(self, rows: list[dict]) -> None:
        collector.write_rows_to_databases(self.data_dir, rows)

    def test_load_candidates_uses_collection_window_and_listing_fields(self):
        self.write_rows(
            [
                listing_row("old", "Old message", "2026-08-31T10:00:00Z"),
                listing_row(
                    "event",
                    "BYBIT: New listing: TMXUSDT Perpetual Contract",
                    "2026-08-31T11:00:00Z",
                    assets=["TMX"],
                    link="https://example.com/event",
                ),
            ]
        )

        with sqlite3.connect(self.database_path) as connection:
            candidates = listing_enricher.load_candidates(
                connection,
                after_collected_at="2026-08-31T10:00:00Z",
                through_collected_at="2026-08-31T11:00:00Z",
            )

        self.assertEqual(
            candidates,
            [
                {
                    "id": "event",
                    "announced_at": "2026-08-31T11:00:00Z",
                    "exchange": "bybit",
                    "text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                    "model_text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                    "assets": ["TMX"],
                    "link": "https://example.com/event",
                }
            ],
        )

    def test_parse_classifications_accepts_event_and_non_event(self):
        candidates = [
            {
                "id": "event",
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "bybit",
                "text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                "model_text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                "assets": ["TMX"],
                "link": "https://example.com/event",
            },
            {
                "id": "other",
                "announced_at": "2026-08-31T12:00:00Z",
                "exchange": "binance",
                "text": "Binance: Enjoy 25% off travel bookings",
                "model_text": "Binance: Enjoy 25% off travel bookings",
                "assets": [],
                "link": None,
            },
        ]
        response = classification_response(
            [
                {
                    "id": "event",
                    "is_listing_event": True,
                    "action": "launch_perpetual",
                    "product_type": "perpetual",
                    "base_assets": ["TMX"],
                    "quote_assets": ["USDT"],
                    "pairs": ["TMXUSDT"],
                    "effective_at": None,
                    "leverage": None,
                    "confidence": 0.99,
                    "evidence": "New listing: TMXUSDT Perpetual Contract",
                    "requires_link_fetch": False,
                },
                {"id": "other", "is_listing_event": False},
            ]
        )

        parsed = listing_enricher.parse_classifications(response, candidates)

        self.assertTrue(parsed["event"]["is_listing_event"])
        self.assertEqual(parsed["event"]["exchange"], "bybit")
        self.assertFalse(parsed["other"]["is_listing_event"])

    def test_parse_classifications_rejects_evidence_not_in_source_text(self):
        candidates = [
            {
                "id": "event",
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "bybit",
                "text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                "model_text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                "assets": ["TMX"],
                "link": None,
            }
        ]
        response = classification_response(
            [
                {
                    "id": "event",
                    "is_listing_event": True,
                    "action": "launch_perpetual",
                    "product_type": "perpetual",
                    "base_assets": ["TMX"],
                    "quote_assets": ["USDT"],
                    "pairs": ["TMXUSDT"],
                    "effective_at": None,
                    "leverage": None,
                    "confidence": 0.99,
                    "evidence": "TMX spot trading opens tomorrow",
                    "requires_link_fetch": False,
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "evidence"):
            listing_enricher.parse_classifications(response, candidates)

    def test_parse_classifications_rejects_invented_structured_facts(self):
        candidates = [
            {
                "id": "event",
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "bybit",
                "text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                "model_text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                "assets": ["TMX"],
                "link": None,
            }
        ]
        response = classification_response(
            [
                {
                    "id": "event",
                    "is_listing_event": True,
                    "action": "launch_perpetual",
                    "product_type": "perpetual",
                    "base_assets": ["BTC"],
                    "quote_assets": ["USDT"],
                    "pairs": ["BTCUSDT"],
                    "effective_at": "2026-09-04",
                    "leverage": "20x",
                    "confidence": 0.99,
                    "evidence": "New listing: TMXUSDT Perpetual Contract",
                    "requires_link_fetch": False,
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "lacked source evidence"):
            listing_enricher.parse_classifications(response, candidates)

    def test_parse_classifications_rejects_listing_action_for_delisting_text(self):
        text = "Binance will delist ICX on 2026-09-03"
        candidates = [
            {
                "id": "event",
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "binance",
                "text": text,
                "model_text": text,
                "assets": ["ICX"],
                "link": None,
            }
        ]
        response = classification_response(
            [
                {
                    "id": "event",
                    "is_listing_event": True,
                    "action": "listing",
                    "product_type": "unknown",
                    "base_assets": ["ICX"],
                    "quote_assets": [],
                    "pairs": [],
                    "effective_at": "2026-09-03",
                    "leverage": None,
                    "confidence": 0.9,
                    "evidence": "delist ICX",
                    "requires_link_fetch": False,
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "action.*lacked source evidence"):
            listing_enricher.parse_classifications(response, candidates)

    def test_parse_classifications_rejects_unmentioned_pair_from_separate_assets(self):
        text = "Binance lists BTC while a USDT rewards campaign continues"
        candidates = [
            {
                "id": "event",
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "binance",
                "text": text,
                "model_text": text,
                "assets": ["BTC", "USDT"],
                "link": None,
            }
        ]
        response = classification_response(
            [
                {
                    "id": "event",
                    "is_listing_event": True,
                    "action": "listing",
                    "product_type": "unknown",
                    "base_assets": ["BTC"],
                    "quote_assets": ["USDT"],
                    "pairs": ["BTCUSDT"],
                    "effective_at": None,
                    "leverage": None,
                    "confidence": 0.9,
                    "evidence": "Binance lists BTC",
                    "requires_link_fetch": False,
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "pair BTCUSDT.*lacked source evidence"):
            listing_enricher.parse_classifications(response, candidates)

    def test_cleaned_html_evidence_matches_model_input(self):
        raw_text = "Bithumb: New&nbsp;listing:<br/>UB/KRW spot market"
        model_text = listing_enricher.clean_text(raw_text)
        candidates = [
            {
                "id": "event",
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "bithumb",
                "text": raw_text,
                "model_text": model_text,
                "assets": ["UB"],
                "link": None,
            }
        ]
        response = classification_response(
            [
                {
                    "id": "event",
                    "is_listing_event": True,
                    "action": "listing",
                    "product_type": "spot",
                    "base_assets": ["UB"],
                    "quote_assets": ["KRW"],
                    "pairs": ["UB/KRW"],
                    "effective_at": None,
                    "leverage": None,
                    "confidence": 0.9,
                    "evidence": "New listing:\nUB/KRW spot market",
                    "requires_link_fetch": False,
                }
            ]
        )

        parsed = listing_enricher.parse_classifications(response, candidates)

        self.assertEqual(parsed["event"]["pairs"], ["UB/KRW"])

    def test_effective_time_accepts_common_english_date_formats(self):
        examples = (
            "Trading opens on 6 August 2026",
            "Trading opens Aug 6, 2026",
            "Trading opens August 6",
            "Trading opens on 6th Aug 2026",
        )

        for source_text in examples:
            with self.subTest(source_text=source_text):
                self.assertTrue(
                    listing_enricher.time_is_evidenced(
                        "2026-08-06",
                        source_text,
                        "2026-08-01T00:00:00Z",
                    )
                )

        self.assertFalse(
            listing_enricher.time_is_evidenced(
                "2027-08-06",
                "Trading opens August 6",
                "2026-08-01T00:00:00Z",
            )
        )
        self.assertTrue(
            listing_enricher.time_is_evidenced(
                "2027-08-06",
                "Trading opens August 6",
                "2026-08-31T00:00:00Z",
            )
        )

    def test_request_classifications_retries_invalid_model_output(self):
        candidates = [
            {
                "id": "event",
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "bybit",
                "text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                "model_text": "BYBIT: New listing: TMXUSDT Perpetual Contract",
                "assets": ["TMX"],
                "link": None,
            }
        ]
        invalid_response = classification_response(
            [{"id": "event", "is_listing_event": "yes"}]
        )
        valid_response = classification_response(
            [{"id": "event", "is_listing_event": False}]
        )

        with mock.patch.object(
            listing_enricher,
            "post_json",
            side_effect=[invalid_response, valid_response],
        ) as post_json:
            result = listing_enricher.request_classifications(
                "https://api.deepseek.com/chat/completions",
                "test-key",
                "deepseek-v4-flash",
                candidates,
                timeout=60,
                max_tokens=32_768,
            )

        self.assertFalse(result["event"]["is_listing_event"])
        self.assertEqual(post_json.call_count, 2)

    def test_truncated_listing_batch_is_split(self):
        candidates = [
            {
                "id": str(item_id),
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "binance",
                "text": f"Binance message {item_id}",
                "model_text": f"Binance message {item_id}",
                "assets": [],
                "link": None,
            }
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
            return classification_response(
                [
                    {"id": request_item["id"], "is_listing_event": False}
                    for request_item in request_items
                ]
            )

        with mock.patch.object(
            listing_enricher,
            "post_json",
            side_effect=fake_post_json,
        ) as post_json:
            batches = list(
                listing_enricher.request_classification_batches(
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

    def test_apply_classifications_deduplicates_event_and_keeps_sources(self):
        text = "Bithumb LISTING: Unibase (UB) new KRW market"
        candidates = [
            {
                "id": "1",
                "announced_at": "2026-08-31T11:00:00Z",
                "exchange": "bithumb",
                "text": text,
                "model_text": text,
                "assets": ["UB"],
                "link": "https://example.com/1",
            },
            {
                "id": "2",
                "announced_at": "2026-08-31T11:01:00Z",
                "exchange": "bithumb",
                "text": text,
                "model_text": text,
                "assets": ["UB"],
                "link": "https://example.com/2",
            },
        ]
        event = {
            "is_listing_event": True,
            "exchange": "bithumb",
            "action": "listing",
            "product_type": "spot",
            "base_assets": ["UB"],
            "quote_assets": ["KRW"],
            "pairs": ["UB/KRW"],
            "effective_at": None,
            "leverage": None,
            "confidence": 0.98,
            "evidence": "Bithumb LISTING: Unibase (UB) new KRW market",
            "requires_link_fetch": False,
        }
        classifications = {
            "1": {"id": "1", **event},
            "2": {
                "id": "2",
                **event,
                "base_assets": [],
                "quote_assets": [],
                "pairs": ["UB-KRW"],
                "leverage": "20x",
            },
        }

        self.write_rows(
            [
                listing_row("1", text, "2026-08-31T11:00:00Z", exchange="bithumb"),
                listing_row("2", text, "2026-08-31T11:01:00Z", exchange="bithumb"),
            ]
        )
        with sqlite3.connect(self.database_path) as connection:
            result = listing_enricher.apply_classifications(
                connection,
                candidates,
                classifications,
                model="deepseek-v4-flash",
                now="2026-08-31T12:00:00Z",
            )
            event_count = connection.execute(
                "SELECT count(*) FROM listing_events"
            ).fetchone()[0]
            source_count = connection.execute(
                "SELECT count(*) FROM listing_event_sources"
            ).fetchone()[0]
            source_payload = json.loads(
                connection.execute(
                    "SELECT ai_payload_json FROM listing_event_sources "
                    "WHERE item_id = '1'"
                ).fetchone()[0]
            )
            leverage = connection.execute(
                "SELECT leverage FROM listing_events"
            ).fetchone()[0]

        self.assertEqual(result["confirmed_events"], 2)
        self.assertEqual(result["events_inserted"], 1)
        self.assertEqual(result["events_updated"], 1)
        self.assertEqual(event_count, 1)
        self.assertEqual(source_count, 2)
        self.assertEqual(leverage, "20x")
        self.assertEqual(source_payload["model"], "deepseek-v4-flash")
        self.assertEqual(source_payload["classification"]["pairs"], ["UB/KRW"])

    def test_apply_classifications_skips_item_changed_after_ai_request(self):
        original_text = "BYBIT: New listing: TMXUSDT Perpetual Contract"
        self.write_rows(
            [listing_row("1", original_text, "2026-08-31T11:00:00Z", assets=["TMX"])]
        )
        candidate = {
            "id": "1",
            "announced_at": "2026-08-31T11:00:00Z",
            "exchange": "bybit",
            "text": original_text,
            "model_text": original_text,
            "assets": ["TMX"],
            "link": None,
        }
        classification = {
            "id": "1",
            "is_listing_event": True,
            "exchange": "bybit",
            "action": "launch_perpetual",
            "product_type": "perpetual",
            "base_assets": ["TMX"],
            "quote_assets": ["USDT"],
            "pairs": ["TMXUSDT"],
            "effective_at": None,
            "leverage": None,
            "confidence": 0.99,
            "evidence": "New listing: TMXUSDT Perpetual Contract",
            "requires_link_fetch": False,
        }
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE items SET text = ? WHERE id = '1'",
                ("Corrected source text",),
            )
            result = listing_enricher.apply_classifications(
                connection,
                [candidate],
                {"1": classification},
                model="deepseek-v4-flash",
                now="2026-08-31T12:00:00Z",
            )
            event_count = connection.execute(
                "SELECT count(*) FROM listing_events"
            ).fetchone()[0]

        self.assertEqual(result["confirmed_events"], 0)
        self.assertEqual(event_count, 0)

    def test_run_classifies_window_and_advances_cursor(self):
        self.write_rows(
            [
                listing_row(
                    "event",
                    "BYBIT: New listing: TMXUSDT Perpetual Contract",
                    "2026-08-31T11:00:00Z",
                    assets=["TMX"],
                ),
                listing_row(
                    "other",
                    "Binance: Enjoy 25% off travel bookings",
                    "2026-08-31T11:00:00Z",
                    exchange="binance",
                ),
            ]
        )
        response = classification_response(
            [
                {
                    "id": "event",
                    "is_listing_event": True,
                    "action": "launch_perpetual",
                    "product_type": "perpetual",
                    "base_assets": ["TMX"],
                    "quote_assets": ["USDT"],
                    "pairs": ["TMXUSDT"],
                    "effective_at": None,
                    "leverage": None,
                    "confidence": 0.99,
                    "evidence": "New listing: TMXUSDT Perpetual Contract",
                    "requires_link_fetch": False,
                },
                {"id": "other", "is_listing_event": False},
            ]
        )
        args = listing_enricher.parse_args(["--data-dir", str(self.data_dir)])

        with mock.patch.dict(
            "os.environ",
            {"DEEPSEEK_API_KEY": "test-key", "OPENNEWS_DATA_DIR": str(self.data_dir)},
            clear=False,
        ):
            with mock.patch.object(
                listing_enricher,
                "post_json",
                return_value=response,
            ) as post_json:
                summary = listing_enricher.run(args)

        self.assertEqual(summary["selected_items"], 2)
        self.assertEqual(summary["confirmed_events"], 1)
        self.assertEqual(summary["non_events"], 1)
        request_body = post_json.call_args.args[2]
        self.assertEqual(request_body["model"], "deepseek-v4-flash")
        self.assertEqual(request_body["reasoning_effort"], "high")
        self.assertEqual(request_body["max_tokens"], 32_768)
        state = json.loads(
            (self.data_dir / "state" / "listing_enrichment.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["through_collected_at"], "2026-08-31T11:00:00Z")


if __name__ == "__main__":
    unittest.main()
