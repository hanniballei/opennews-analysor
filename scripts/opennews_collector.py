#!/usr/bin/env python3
"""Collect selected 6551 OpenNews feeds into local JSONL files."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_API_BASE_URL = "https://ai.6551.io"
DEFAULT_DATA_DIR = "/root/trading/data/opennews"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
POINT_RECORDS = 20


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_event_time(item: dict[str, Any], fallback: datetime) -> datetime:
    for key in ("published_at", "created_at", "ts", "timestamp"):
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            text = value.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return fallback


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


def normalize_item(
    item: dict[str, Any],
    item_id: str,
    collected_at: datetime,
    profile: str,
) -> dict[str, Any]:
    ai_rating = item.get("aiRating") or {}
    title = item.get("title") or item.get("text") or ""
    event_time = parse_event_time(item, collected_at)

    return {
        "id": item_id,
        "profile": profile,
        "collected_at": iso_utc(collected_at),
        "event_date": event_time.date().isoformat(),
        "published_at": item.get("published_at") or item.get("ts"),
        "created_at": item.get("created_at"),
        "source": item.get("source") or item.get("newsType"),
        "engine_type": item.get("engine_type") or item.get("engineType"),
        "title": title,
        "text": item.get("text") or title,
        "link": item.get("link"),
        "assets": extract_assets(item.get("coins")),
        "score": item.get("score", ai_rating.get("score")),
        "grade": item.get("grade", ai_rating.get("grade")),
        "signal": item.get("signal", ai_rating.get("signal")),
        "summary_zh": item.get("summary_zh") or ai_rating.get("summary"),
        "summary_en": item.get("summary_en") or ai_rating.get("enSummary"),
        "ai_rating": ai_rating or None,
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


def append_jsonl_by_date(root: Path, rows: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["event_date"]].append(row)

    for event_date, day_rows in grouped.items():
        year, month, day = event_date.split("-")
        path = root / "normalized" / year / month / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in day_rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                fh.write("\n")


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
    next_pages = payload.get("next_pages", args.min_pages)
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
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "success": True,
                "skipped": True,
                "reason": "another collector run is active",
            }

        collected_at = utc_now()
        historical_seen_ids = load_seen_ids(seen_path)
        seen_ids = set(historical_seen_ids)
        new_rows: list[dict[str, Any]] = []
        counts_by_engine: dict[str, int] = defaultdict(int)
        fetched_counts_by_engine: dict[str, int] = defaultdict(int)
        counts_by_profile: dict[str, int] = defaultdict(int)
        raw_pages: list[dict[str, Any]] = []
        pages_fetched = 0
        points_used = 0
        historical_items = 0
        intra_run_duplicates = 0
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
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = stable_item_id(item)
                item_engine_type = str(item.get("engine_type") or item.get("engineType") or "")
                fetched_counts_by_engine[item_engine_type] += 1
                event_time = parse_event_time(item, collected_at)
                oldest_fetched_at = event_time if oldest_fetched_at is None else min(oldest_fetched_at, event_time)
                newest_fetched_at = event_time if newest_fetched_at is None else max(newest_fetched_at, event_time)
                if item_id in historical_seen_ids:
                    page_historical_items += 1
                    historical_items += 1
                    continue
                if item_id in seen_ids:
                    intra_run_duplicates += 1
                    page_intra_run_duplicates += 1
                    continue
                seen_ids.add(item_id)
                item_profile = profile_for_item(item, args)
                new_rows.append(normalize_item(item, item_id, collected_at, item_profile))
                counts_by_engine[item_engine_type] += 1
                counts_by_profile[item_profile] += 1
                page_new += 1

            raw_page["new_items"] = page_new
            raw_page["historical_items"] = page_historical_items
            raw_page["intra_run_duplicates"] = page_intra_run_duplicates

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
                    "boundary_confirmed": boundary_confirmed,
                    "saturated": saturated,
                    "oldest_fetched_at": iso_utc(oldest_fetched_at) if oldest_fetched_at else None,
                    "newest_fetched_at": iso_utc(newest_fetched_at) if newest_fetched_at else None,
                    "pages": raw_pages,
                },
            )

        if new_rows and not args.dry_run:
            append_jsonl_by_date(data_dir, new_rows)
            save_seen_ids(seen_path, seen_ids, args.seen_retention)

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
            "boundary_confirmed": boundary_confirmed,
            "saturated": saturated,
            "stopped_on_known_item": stopped_on_known_item,
            "stopped_on_known_page": stopped_on_known_page,
            "stopped_on_empty_page": stopped_on_empty_page,
            "new_items": len(new_rows),
            "counts_by_engine": dict(sorted(counts_by_engine.items())),
            "fetched_counts_by_engine": dict(sorted(fetched_counts_by_engine.items())),
            "missing_requested_engines": missing_requested_engines,
            "counts_by_profile": dict(sorted(counts_by_profile.items())),
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
    parser.add_argument("--adaptive-pages", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--seen-retention", type=int, default=250_000)
    parser.add_argument("--stop-after-known-pages", type=int, default=1)
    parser.add_argument("--stop-on-known-item", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--no-raw", action="store_false", dest="write_raw")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.limit = max(1, min(args.limit, MAX_LIMIT))
    if args.min_score is not None:
        args.min_score = max(0, min(args.min_score, 100))
    args.start_page = max(1, args.start_page)
    args.max_pages = max(1, args.max_pages)
    args.min_pages = max(1, min(args.min_pages, args.max_pages))
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
