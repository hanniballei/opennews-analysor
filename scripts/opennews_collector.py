#!/usr/bin/env python3
"""Collect selected 6551 OpenNews feeds into local JSONL files."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_API_BASE_URL = "https://ai.6551.io"
DEFAULT_DATA_DIR = "/root/trading/data/opennews"
MAX_LIMIT = 100
MAX_PAGE = 100
DEFAULT_LIMIT = 20
POINT_RECORDS = 20
SCHEMA_VERSION = 3
INCOMPLETE_AI_STATUSES = {"pending", "processing"}
DATABASE_ENGINES = ("news", "listing")
ISO_TIMESTAMP_RE = re.compile(
    r"^(?P<prefix>.+T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d+))?(?P<timezone>Z|[+-]\d{2}:\d{2})?$"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: str) -> datetime | None:
    text = value.strip()
    match = ISO_TIMESTAMP_RE.fullmatch(text)
    if match:
        fraction = match.group("fraction")
        if fraction is not None:
            fraction = fraction[:6].ljust(6, "0")
        text = match.group("prefix")
        if fraction is not None:
            text += f".{fraction}"
        text += match.group("timezone") or ""
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_event_time_details(
    item: dict[str, Any],
    fallback: datetime,
) -> tuple[datetime, str | None, Any]:
    for key in ("published_at", "created_at", "ts", "timestamp"):
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc), key, value
        if isinstance(value, str) and value.strip():
            parsed = parse_iso_datetime(value)
            if parsed is not None:
                return parsed, key, value
    return fallback, "collected_at", iso_utc(fallback)


def parse_event_time(item: dict[str, Any], fallback: datetime) -> datetime:
    return parse_event_time_details(item, fallback)[0]


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def load_seen_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    ids = payload.get("ids", []) if isinstance(payload, dict) else payload
    return {str(item) for item in ids}


def seen_item_key(engine_type: str, item_id: str) -> str:
    return f"{engine_type}:{item_id}"


def is_historically_seen(
    historical_seen_ids: set[str],
    engine_type: str,
    item_id: str,
) -> bool:
    if seen_item_key(engine_type, item_id) in historical_seen_ids:
        return True
    return engine_type == "news" and item_id in historical_seen_ids


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload if isinstance(payload, dict) else {}


def save_seen_ids(path: Path, seen_ids: set[str], retention: int) -> None:
    ids = sorted(seen_ids)
    if retention > 0 and len(ids) > retention:
        ids = ids[-retention:]
    atomic_write_json(
        path,
        {
            "updated_at": iso_utc(utc_now()),
            "count": len(ids),
            "ids": ids,
        },
    )


def stable_item_id(item: dict[str, Any]) -> str:
    for key in ("id", "_id", "news_id", "newsId"):
        value = item.get(key)
        if value is not None and value != "":
            return str(value)

    parts = [
        str(item.get("source") or item.get("newsType") or ""),
        str(item.get("engine_type") or item.get("engineType") or ""),
        str(item.get("published_at") or item.get("created_at") or item.get("ts") or ""),
        str(item.get("link") or ""),
        str(item.get("title") or item.get("text") or ""),
    ]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def extract_assets(coins: Any) -> list[str]:
    if not coins:
        return []
    assets: list[str] = []
    if isinstance(coins, list):
        for item in coins:
            if isinstance(item, str):
                assets.append(item)
            elif isinstance(item, dict):
                symbol = item.get("symbol") or item.get("coin") or item.get("name")
                if symbol:
                    assets.append(str(symbol))
    return assets


def extract_asset_details(coins: Any) -> list[dict[str, Any]]:
    if not isinstance(coins, list):
        return []
    details: list[dict[str, Any]] = []
    for item in coins:
        if isinstance(item, dict):
            details.append(dict(item))
        elif isinstance(item, str):
            details.append({"symbol": item})
    return details


def normalized_content(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split())


def content_fingerprint(item: dict[str, Any]) -> str:
    content = normalized_content(item.get("title") or item.get("text"))
    if not content:
        content = "\n".join(
            str(item.get(key) or "")
            for key in ("source", "newsType", "ts", "link")
        )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def should_defer_item(item: dict[str, Any]) -> bool:
    ai_rating = item.get("aiRating")
    if not isinstance(ai_rating, dict):
        return False
    status = str(ai_rating.get("status") or "").casefold()
    return status in INCOMPLETE_AI_STATUSES


def prefer_value(primary: Any, fallback: Any) -> Any:
    return fallback if primary is None else primary


def normalize_item(
    item: dict[str, Any],
    item_id: str,
    collected_at: datetime,
    profile: str,
) -> dict[str, Any]:
    ai_rating = item.get("aiRating") or {}
    title = item.get("title") or item.get("text") or ""
    event_time, event_time_source, event_time_raw = parse_event_time_details(item, collected_at)
    raw_source = item.get("source") or None
    news_type = item.get("newsType") or None

    return {
        "schema_version": SCHEMA_VERSION,
        "id": item_id,
        "profile": profile,
        "collected_at": iso_utc(collected_at),
        "event_date": event_time.date().isoformat(),
        "event_time": iso_utc(event_time),
        "event_time_source": event_time_source,
        "event_time_raw": event_time_raw,
        "published_at": item.get("published_at") or item.get("publishedAt"),
        "created_at": item.get("created_at") or item.get("createdAt"),
        "source": raw_source or news_type,
        "source_raw": raw_source,
        "news_type": news_type,
        "engine_type": item.get("engine_type") or item.get("engineType"),
        "title": title,
        "title_source": "title" if item.get("title") else "text" if item.get("text") else None,
        "text": item.get("text") or title,
        "description": item.get("description"),
        "link": item.get("link"),
        "assets": extract_assets(item.get("coins")),
        "asset_details": extract_asset_details(item.get("coins")),
        "score": prefer_value(item.get("score"), ai_rating.get("score")),
        "grade": prefer_value(item.get("grade"), ai_rating.get("grade")),
        "signal": prefer_value(item.get("signal"), ai_rating.get("signal")),
        "summary_zh": item.get("summary_zh") or ai_rating.get("summary"),
        "summary_en": item.get("summary_en") or ai_rating.get("enSummary"),
        "ai_status": ai_rating.get("status"),
        "ai_rating": ai_rating or None,
        "content_fingerprint": content_fingerprint(item),
        "raw": item,
    }


def profile_for_item(item: dict[str, Any], args: argparse.Namespace) -> str:
    if not args.split_profile_by_engine:
        return args.profile
    engine_type = item.get("engine_type") or item.get("engineType")
    return str(engine_type or args.profile)


def post_json(url: str, token: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "opennews-collector/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenNews API returned HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenNews API request failed: {exc}") from exc


def append_jsonl_by_date(root: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["event_date"]].append(row)

    written_rows: list[dict[str, Any]] = []
    for event_date, day_rows in grouped.items():
        year, month, day = event_date.split("-")
        path = root / "normalized" / year / month / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_keys: set[str] = set()
        existing_lines: list[str] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    existing_lines.append(line)
                    existing_row = json.loads(line)
                    existing_id = existing_row.get("id")
                    if existing_id is not None:
                        existing_engine = str(
                            existing_row.get("engine_type")
                            or existing_row.get("profile")
                            or ""
                        )
                        existing_keys.add(f"{existing_engine}:{existing_id}")
        pending_rows: list[dict[str, Any]] = []
        for row in day_rows:
            row_engine = str(row.get("engine_type") or row.get("profile") or "")
            row_key = f"{row_engine}:{row.get('id')}"
            if row_key in existing_keys:
                continue
            existing_keys.add(row_key)
            pending_rows.append(row)
        if not pending_rows:
            continue
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as fh:
            for line in existing_lines:
                fh.write(line)
                if not line.endswith("\n"):
                    fh.write("\n")
            for row in pending_rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                fh.write("\n")
                written_rows.append(row)
            tmp_path = Path(fh.name)
        os.replace(tmp_path, path)
    return written_rows


def append_usage_record(root: Path, collected_at: datetime, summary: dict[str, Any]) -> None:
    path = (
        root
        / "usage"
        / collected_at.strftime("%Y")
        / collected_at.strftime("%m")
        / f"{collected_at:%d}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)


def database_path(root: Path, engine_type: str) -> Path:
    return root / "databases" / f"{safe_name(engine_type)}.sqlite3"


def ensure_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                profile TEXT NOT NULL,
                engine_type TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                event_date TEXT NOT NULL,
                event_time TEXT,
                event_time_source TEXT,
                event_time_raw_json TEXT,
                published_at TEXT,
                created_at TEXT,
                source TEXT,
                source_raw TEXT,
                news_type TEXT,
                title TEXT NOT NULL,
                title_source TEXT,
                text TEXT NOT NULL,
                description TEXT,
                link TEXT,
                assets_json TEXT NOT NULL,
                asset_details_json TEXT NOT NULL,
                score REAL,
                grade TEXT,
                signal TEXT,
                summary_zh TEXT,
                summary_en TEXT,
                ai_status TEXT,
                content_fingerprint TEXT,
                quality_rank INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                inserted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS items_collected_at_idx ON items(collected_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS items_event_time_idx ON items(event_time)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS items_score_idx ON items(score)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS items_fingerprint_idx ON items(content_fingerprint)"
        )


def row_quality_rank(row: dict[str, Any]) -> int:
    status = str(row.get("ai_status") or "").casefold()
    return {"done": 3, "processing": 1, "pending": 0}.get(status, 2)


def database_record(row: dict[str, Any], now: str) -> dict[str, Any]:
    structured_payload = dict(row)
    structured_payload.pop("raw", None)
    return {
        "id": str(row.get("id")),
        "schema_version": int(row.get("schema_version") or 1),
        "profile": str(row.get("profile") or row.get("engine_type") or ""),
        "engine_type": str(row.get("engine_type") or row.get("profile") or ""),
        "collected_at": str(row.get("collected_at") or now),
        "event_date": str(row.get("event_date") or ""),
        "event_time": row.get("event_time"),
        "event_time_source": row.get("event_time_source"),
        "event_time_raw_json": json.dumps(row.get("event_time_raw"), ensure_ascii=False),
        "published_at": row.get("published_at"),
        "created_at": row.get("created_at"),
        "source": row.get("source"),
        "source_raw": row.get("source_raw"),
        "news_type": row.get("news_type"),
        "title": str(row.get("title") or ""),
        "title_source": row.get("title_source"),
        "text": str(row.get("text") or row.get("title") or ""),
        "description": row.get("description"),
        "link": row.get("link"),
        "assets_json": json.dumps(row.get("assets") or [], ensure_ascii=False),
        "asset_details_json": json.dumps(row.get("asset_details") or [], ensure_ascii=False),
        "score": row.get("score"),
        "grade": row.get("grade"),
        "signal": row.get("signal"),
        "summary_zh": row.get("summary_zh"),
        "summary_en": row.get("summary_en"),
        "ai_status": row.get("ai_status"),
        "content_fingerprint": row.get("content_fingerprint"),
        "quality_rank": row_quality_rank(row),
        "payload_json": json.dumps(structured_payload, ensure_ascii=False, sort_keys=True),
        "raw_json": json.dumps(row.get("raw") or {}, ensure_ascii=False, sort_keys=True),
        "inserted_at": now,
        "updated_at": now,
    }


DATABASE_UPSERT_SQL = """
INSERT INTO items (
    id, schema_version, profile, engine_type, collected_at, event_date,
    event_time, event_time_source, event_time_raw_json, published_at, created_at,
    source, source_raw, news_type, title, title_source, text, description, link,
    assets_json, asset_details_json, score, grade, signal, summary_zh, summary_en,
    ai_status, content_fingerprint, quality_rank, payload_json, raw_json,
    inserted_at, updated_at
) VALUES (
    :id, :schema_version, :profile, :engine_type, :collected_at, :event_date,
    :event_time, :event_time_source, :event_time_raw_json, :published_at, :created_at,
    :source, :source_raw, :news_type, :title, :title_source, :text, :description, :link,
    :assets_json, :asset_details_json, :score, :grade, :signal, :summary_zh, :summary_en,
    :ai_status, :content_fingerprint, :quality_rank, :payload_json, :raw_json,
    :inserted_at, :updated_at
)
ON CONFLICT(id) DO UPDATE SET
    schema_version = excluded.schema_version,
    profile = excluded.profile,
    engine_type = excluded.engine_type,
    collected_at = CASE
        WHEN excluded.collected_at < items.collected_at THEN excluded.collected_at
        ELSE items.collected_at
    END,
    event_date = excluded.event_date,
    event_time = excluded.event_time,
    event_time_source = excluded.event_time_source,
    event_time_raw_json = excluded.event_time_raw_json,
    published_at = excluded.published_at,
    created_at = excluded.created_at,
    source = excluded.source,
    source_raw = excluded.source_raw,
    news_type = excluded.news_type,
    title = excluded.title,
    title_source = excluded.title_source,
    text = excluded.text,
    description = excluded.description,
    link = excluded.link,
    assets_json = excluded.assets_json,
    asset_details_json = excluded.asset_details_json,
    score = excluded.score,
    grade = excluded.grade,
    signal = excluded.signal,
    summary_zh = excluded.summary_zh,
    summary_en = excluded.summary_en,
    ai_status = excluded.ai_status,
    content_fingerprint = excluded.content_fingerprint,
    quality_rank = excluded.quality_rank,
    payload_json = excluded.payload_json,
    raw_json = excluded.raw_json,
    updated_at = excluded.updated_at
WHERE excluded.schema_version > items.schema_version
   OR (
       excluded.schema_version = items.schema_version
       AND (
           excluded.quality_rank > items.quality_rank
           OR (
               excluded.quality_rank = items.quality_rank
               AND excluded.raw_json <> items.raw_json
           )
       )
   )
"""


def write_rows_to_databases(
    root: Path,
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    results = {
        engine_type: {"inserted": 0, "updated": 0, "unchanged": 0}
        for engine_type in DATABASE_ENGINES
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        engine_type = str(row.get("engine_type") or row.get("profile") or "")
        if engine_type in DATABASE_ENGINES:
            grouped[engine_type].append(row)

    now = iso_utc(utc_now())
    for engine_type in DATABASE_ENGINES:
        path = database_path(root, engine_type)
        ensure_database(path)
        with sqlite3.connect(path, timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            for row in grouped.get(engine_type, []):
                record = database_record(row, now)
                existing = connection.execute(
                    "SELECT quality_rank, schema_version, raw_json, collected_at FROM items WHERE id = ?",
                    (record["id"],),
                ).fetchone()
                if existing is not None and existing[3] < record["collected_at"]:
                    record["collected_at"] = existing[3]
                    payload = json.loads(record["payload_json"])
                    payload["collected_at"] = existing[3]
                    record["payload_json"] = json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                connection.execute(DATABASE_UPSERT_SQL, record)
                if existing is None:
                    results[engine_type]["inserted"] += 1
                elif record["schema_version"] > existing[1] or (
                    record["schema_version"] == existing[1]
                    and (
                        record["quality_rank"] > existing[0]
                        or (
                            record["quality_rank"] == existing[0]
                            and record["raw_json"] != existing[2]
                        )
                    )
                ):
                    results[engine_type]["updated"] += 1
                else:
                    results[engine_type]["unchanged"] += 1
    return results


def normalize_stored_row(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("raw")
    if not isinstance(raw, dict):
        raw = {
            "id": row.get("id"),
            "engineType": row.get("engine_type") or row.get("profile"),
            "newsType": row.get("news_type"),
            "source": row.get("source_raw") or row.get("source"),
            "title": row.get("title") if row.get("title_source") == "title" else None,
            "text": row.get("text") or row.get("title"),
            "description": row.get("description"),
            "link": row.get("link"),
            "published_at": row.get("published_at"),
            "created_at": row.get("created_at"),
            "coins": row.get("asset_details") or row.get("assets") or [],
            "score": row.get("score"),
            "grade": row.get("grade"),
            "signal": row.get("signal"),
            "aiRating": row.get("ai_rating"),
        }
    engine_type = str(
        row.get("engine_type")
        or raw.get("engine_type")
        or raw.get("engineType")
        or row.get("profile")
        or ""
    )
    if engine_type not in DATABASE_ENGINES:
        return None
    collected_at_value = str(row.get("collected_at") or "")
    collected_at = parse_iso_datetime(collected_at_value)
    if collected_at is None:
        return None
    item_id = str(row.get("id") or stable_item_id(raw))
    return normalize_item(raw, item_id, collected_at, engine_type)


def write_raw_run(root: Path, collected_at: datetime, profile: str, payload: dict[str, Any]) -> Path:
    stamp = collected_at.strftime("%Y%m%d_%H%M%S")
    path = root / "raw" / collected_at.strftime("%Y") / collected_at.strftime("%m") / collected_at.strftime("%d") / f"opennews_{safe_name(profile)}_{stamp}.json"
    atomic_write_json(path, payload)
    return path


def build_engine_types(engine_types: list[str]) -> dict[str, list[str]] | None:
    if not engine_types:
        return None
    return {engine_type: [] for engine_type in engine_types}


def current_page_limit(args: argparse.Namespace, adaptive_path: Path) -> int:
    if not args.adaptive_pages:
        return args.max_pages
    payload = load_json_object(adaptive_path)
    next_pages = payload.get("next_pages", args.initial_pages)
    if not isinstance(next_pages, int):
        next_pages = args.min_pages
    return max(args.min_pages, min(args.max_pages, next_pages))


def next_page_limit(
    args: argparse.Namespace,
    page_limit: int,
    pages_fetched: int,
    stopped_on_known_page: bool,
    stopped_on_empty_page: bool,
) -> int:
    if not args.adaptive_pages:
        return args.max_pages
    if stopped_on_known_page or stopped_on_empty_page:
        return max(args.min_pages, min(args.max_pages, pages_fetched + 1))
    return args.max_pages


def collect(args: argparse.Namespace) -> dict[str, Any]:
    token = os.environ.get("OPENNEWS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("OPENNEWS_TOKEN is required")

    api_base_url = os.environ.get("OPENNEWS_API_BASE_URL", args.api_base_url).rstrip("/")
    data_dir = Path(os.environ.get("OPENNEWS_DATA_DIR", args.data_dir)).expanduser()
    state_dir = data_dir / "state"
    lock_path = state_dir / "collector.lock"
    seen_path = state_dir / "seen_ids.json"
    last_run_path = state_dir / "last_run.json"
    profile_last_run_path = state_dir / f"last_run_{safe_name(args.profile)}.json"
    adaptive_path = state_dir / f"adaptive_{safe_name(args.profile)}.json"
    state_dir.mkdir(parents=True, exist_ok=True)

    with lock_path.open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)

        collected_at = utc_now()
        historical_seen_ids = load_seen_ids(seen_path)
        seen_ids = set(historical_seen_ids)
        new_rows: list[dict[str, Any]] = []
        database_rows: list[dict[str, Any]] = []
        fetched_counts_by_engine: dict[str, int] = defaultdict(int)
        raw_pages: list[dict[str, Any]] = []
        pages_fetched = 0
        points_used = 0
        historical_items = 0
        intra_run_duplicates = 0
        deferred_items = 0
        known_pages = 0
        stopped_on_known_page = False
        stopped_on_known_item = False
        stopped_on_empty_page = False
        api_url = f"{api_base_url}/open/news_search"
        engine_types = build_engine_types(args.engine_type)
        page_limit = current_page_limit(args, adaptive_path)
        oldest_fetched_at: datetime | None = None
        newest_fetched_at: datetime | None = None

        for page in range(args.start_page, args.start_page + page_limit):
            body = {"limit": args.limit, "page": page}
            if engine_types:
                body["engineTypes"] = engine_types
            if args.min_score is not None:
                body["score"] = args.min_score
            result = post_json(api_url, token, body, args.timeout)
            items = result.get("data", [])
            if not isinstance(items, list):
                raise RuntimeError(f"OpenNews API returned unexpected data field: {type(items).__name__}")

            pages_fetched += 1
            points_used += (len(items) + POINT_RECORDS - 1) // POINT_RECORDS
            raw_page = {
                "page": page,
                "request": body,
                "total": result.get("total"),
                "count": len(items),
                "points_used": (len(items) + POINT_RECORDS - 1) // POINT_RECORDS,
                "data": items,
                "response_meta": {key: value for key, value in result.items() if key != "data"},
            }
            raw_pages.append(raw_page)

            page_new = 0
            page_historical_items = 0
            page_intra_run_duplicates = 0
            page_deferred_items = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = stable_item_id(item)
                item_engine_type = str(item.get("engine_type") or item.get("engineType") or "")
                item_seen_key = seen_item_key(item_engine_type, item_id)
                fetched_counts_by_engine[item_engine_type] += 1
                event_time = parse_event_time(item, collected_at)
                oldest_fetched_at = event_time if oldest_fetched_at is None else min(oldest_fetched_at, event_time)
                newest_fetched_at = event_time if newest_fetched_at is None else max(newest_fetched_at, event_time)
                if item_engine_type != "listing" and should_defer_item(item):
                    deferred_items += 1
                    page_deferred_items += 1
                    continue
                item_profile = profile_for_item(item, args)
                normalized_row = normalize_item(item, item_id, collected_at, item_profile)
                database_rows.append(normalized_row)
                if is_historically_seen(
                    historical_seen_ids,
                    item_engine_type,
                    item_id,
                ):
                    page_historical_items += 1
                    historical_items += 1
                    continue
                if item_seen_key in seen_ids:
                    intra_run_duplicates += 1
                    page_intra_run_duplicates += 1
                    continue
                seen_ids.add(item_seen_key)
                new_rows.append(normalized_row)
                page_new += 1

            raw_page["new_items"] = page_new
            raw_page["historical_items"] = page_historical_items
            raw_page["intra_run_duplicates"] = page_intra_run_duplicates
            raw_page["deferred_items"] = page_deferred_items

            if not items:
                stopped_on_empty_page = True
                break
            if args.stop_on_known_item and page_historical_items > 0:
                stopped_on_known_item = True
                break
            if page_new == 0 and page_historical_items > 0:
                known_pages += 1
            else:
                known_pages = 0
            if args.stop_after_known_pages > 0 and known_pages >= args.stop_after_known_pages:
                stopped_on_known_page = True
                break

        next_pages = next_page_limit(
            args,
            page_limit,
            pages_fetched,
            stopped_on_known_page or stopped_on_known_item,
            stopped_on_empty_page,
        )
        boundary_confirmed = stopped_on_known_item or stopped_on_known_page or stopped_on_empty_page
        saturated = pages_fetched >= page_limit and not boundary_confirmed
        missing_requested_engines = sorted(
            engine_type
            for engine_type in args.engine_type
            if fetched_counts_by_engine.get(engine_type, 0) == 0
        )
        raw_path = None
        if args.write_raw:
            raw_path = write_raw_run(
                data_dir,
                collected_at,
                args.profile,
                {
                    "profile": args.profile,
                    "collected_at": iso_utc(collected_at),
                    "api_base_url": api_base_url,
                    "engine_types": engine_types,
                    "min_score": args.min_score,
                    "start_page": args.start_page,
                    "page_limit": page_limit,
                    "pages_fetched": pages_fetched,
                    "points_used": points_used,
                    "new_items": len(new_rows),
                    "fetched_counts_by_engine": dict(sorted(fetched_counts_by_engine.items())),
                    "missing_requested_engines": missing_requested_engines,
                    "historical_items": historical_items,
                    "intra_run_duplicates": intra_run_duplicates,
                    "deferred_items": deferred_items,
                    "boundary_confirmed": boundary_confirmed,
                    "saturated": saturated,
                    "oldest_fetched_at": iso_utc(oldest_fetched_at) if oldest_fetched_at else None,
                    "newest_fetched_at": iso_utc(newest_fetched_at) if newest_fetched_at else None,
                    "pages": raw_pages,
                },
            )

        written_rows = new_rows
        write_duplicate_items = 0
        database_writes = {
            engine_type: {"inserted": 0, "updated": 0, "unchanged": 0}
            for engine_type in DATABASE_ENGINES
        }
        if database_rows and not args.dry_run:
            database_writes = write_rows_to_databases(data_dir, database_rows)
        if new_rows and not args.dry_run:
            written_rows = append_jsonl_by_date(data_dir, new_rows)
            write_duplicate_items = len(new_rows) - len(written_rows)
            save_seen_ids(seen_path, seen_ids, args.seen_retention)
        elif not args.dry_run and not database_rows:
            write_rows_to_databases(data_dir, [])

        summary_rows = new_rows if args.dry_run else written_rows
        summary_counts_by_engine: dict[str, int] = defaultdict(int)
        summary_counts_by_profile: dict[str, int] = defaultdict(int)
        for row in summary_rows:
            summary_counts_by_engine[str(row.get("engine_type") or "")] += 1
            summary_counts_by_profile[str(row.get("profile") or "")] += 1

        if args.adaptive_pages and not args.dry_run:
            atomic_write_json(
                adaptive_path,
                {
                    "profile": args.profile,
                    "min_score": args.min_score,
                    "updated_at": iso_utc(utc_now()),
                    "page_limit": page_limit,
                    "next_pages": next_pages,
                    "pages_fetched": pages_fetched,
                    "points_used": points_used,
                    "fetched_counts_by_engine": dict(sorted(fetched_counts_by_engine.items())),
                    "missing_requested_engines": missing_requested_engines,
                    "historical_items": historical_items,
                    "intra_run_duplicates": intra_run_duplicates,
                    "deferred_items": deferred_items,
                    "write_duplicate_items": write_duplicate_items,
                    "boundary_confirmed": boundary_confirmed,
                    "saturated": saturated,
                    "stopped_on_known_page": stopped_on_known_page,
                    "stopped_on_known_item": stopped_on_known_item,
                    "stopped_on_empty_page": stopped_on_empty_page,
                },
            )

        summary = {
            "success": True,
            "dry_run": args.dry_run,
            "profile": args.profile,
            "engine_types": engine_types,
            "min_score": args.min_score,
            "collected_at": iso_utc(collected_at),
            "start_page": args.start_page,
            "page_limit": page_limit,
            "next_pages": next_pages,
            "pages_fetched": pages_fetched,
            "points_used": points_used,
            "historical_items": historical_items,
            "intra_run_duplicates": intra_run_duplicates,
            "deferred_items": deferred_items,
            "write_duplicate_items": write_duplicate_items,
            "database_writes": database_writes,
            "database_paths": {
                engine_type: str(database_path(data_dir, engine_type))
                for engine_type in DATABASE_ENGINES
            },
            "boundary_confirmed": boundary_confirmed,
            "saturated": saturated,
            "stopped_on_known_item": stopped_on_known_item,
            "stopped_on_known_page": stopped_on_known_page,
            "stopped_on_empty_page": stopped_on_empty_page,
            "new_items": len(summary_rows),
            "counts_by_engine": dict(sorted(summary_counts_by_engine.items())),
            "fetched_counts_by_engine": dict(sorted(fetched_counts_by_engine.items())),
            "missing_requested_engines": missing_requested_engines,
            "counts_by_profile": dict(sorted(summary_counts_by_profile.items())),
            "seen_ids": len(seen_ids),
            "oldest_fetched_at": iso_utc(oldest_fetched_at) if oldest_fetched_at else None,
            "newest_fetched_at": iso_utc(newest_fetched_at) if newest_fetched_at else None,
            "raw_path": str(raw_path) if raw_path else None,
            "data_dir": str(data_dir),
        }
        if not args.dry_run:
            usage_path = (
                data_dir
                / "usage"
                / collected_at.strftime("%Y")
                / collected_at.strftime("%m")
                / f"{collected_at:%d}.jsonl"
            )
            summary["usage_path"] = str(usage_path)
            append_usage_record(data_dir, collected_at, summary)
            atomic_write_json(last_run_path, summary)
            atomic_write_json(profile_last_run_path, summary)
        return summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--engine-type", action="append", default=[])
    parser.add_argument("--split-profile-by-engine", action="store_true")
    parser.add_argument("--min-score", type=int)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--min-pages", type=int, default=3)
    parser.add_argument("--initial-pages", type=int)
    parser.add_argument("--adaptive-pages", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--seen-retention", type=int, default=0)
    parser.add_argument("--stop-after-known-pages", type=int, default=1)
    parser.add_argument("--stop-on-known-item", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--no-raw", action="store_false", dest="write_raw")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.limit = max(1, min(args.limit, MAX_LIMIT))
    if args.min_score is not None:
        args.min_score = max(0, min(args.min_score, 100))
    args.start_page = max(1, min(args.start_page, MAX_PAGE))
    args.max_pages = max(1, min(args.max_pages, MAX_PAGE - args.start_page + 1))
    args.min_pages = max(1, min(args.min_pages, args.max_pages))
    if args.initial_pages is None:
        args.initial_pages = args.min_pages
    args.initial_pages = max(args.min_pages, min(args.initial_pages, args.max_pages))
    args.timeout = max(1, args.timeout)
    return args


def main(argv: list[str]) -> int:
    start = time.monotonic()
    try:
        summary = collect(parse_args(argv))
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    summary["elapsed_seconds"] = round(time.monotonic() - start, 3)
    if summary.get("saturated"):
        print(
            json.dumps(
                {
                    "warning": "page limit reached before a historical or empty-page boundary",
                    "page_limit": summary["page_limit"],
                    "next_pages": summary["next_pages"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    if summary.get("missing_requested_engines"):
        print(
            json.dumps(
                {
                    "warning": "requested engines returned no records",
                    "missing_requested_engines": summary["missing_requested_engines"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
