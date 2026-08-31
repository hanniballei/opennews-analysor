#!/usr/bin/env python3
"""Migrate historical OpenNews JSONL rows into engine-specific SQLite databases."""

from __future__ import annotations

import argparse
import fcntl
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from opennews_collector import DATABASE_ENGINES
from opennews_collector import database_path
from opennews_collector import normalize_item
from opennews_collector import normalize_stored_row
from opennews_collector import parse_iso_datetime
from opennews_collector import stable_item_id
from opennews_collector import write_rows_to_databases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="/root/trading/data/opennews")
    parser.add_argument("--batch-size", type=int, default=1_000)
    return parser.parse_args()


def merge_counts(target: Counter[str], result: dict[str, dict[str, int]]) -> None:
    for engine_type, counts in result.items():
        for status, count in counts.items():
            target[f"{engine_type}_{status}"] += count


def flush_batch(
    data_dir: Path,
    batch: list[dict],
    totals: Counter[str],
) -> None:
    if not batch:
        return
    merge_counts(totals, write_rows_to_databases(data_dir, batch))
    batch.clear()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser()
    normalized_dir = data_dir / "normalized"
    batch_size = max(1, args.batch_size)
    totals: Counter[str] = Counter()
    batch: list[dict] = []

    lock_path = data_dir / "state" / "collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        for path in sorted(normalized_dir.rglob("*.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    totals["rows_scanned"] += 1
                    try:
                        stored_row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
                    row = normalize_stored_row(stored_row)
                    if row is None:
                        totals["rows_skipped"] += 1
                        continue
                    totals[f"{row['engine_type']}_rows"] += 1
                    batch.append(row)
                    if len(batch) >= batch_size:
                        flush_batch(data_dir, batch, totals)

        flush_batch(data_dir, batch, totals)

        raw_dir = data_dir / "raw"
        raw_paths: list[tuple[datetime, Path]] = []
        for path in raw_dir.rglob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            collected_at = parse_iso_datetime(str(payload.get("collected_at") or ""))
            if collected_at is None:
                totals["raw_files_skipped"] += 1
                continue
            raw_paths.append((collected_at, path))

        for collected_at, path in sorted(raw_paths, key=lambda item: item[0]):
            payload = json.loads(path.read_text(encoding="utf-8"))
            totals["raw_files_scanned"] += 1
            for page in payload.get("pages", []):
                if not isinstance(page, dict):
                    continue
                for item in page.get("data", []):
                    totals["raw_occurrences_scanned"] += 1
                    if not isinstance(item, dict):
                        totals["raw_occurrences_skipped"] += 1
                        continue
                    engine_type = str(
                        item.get("engine_type") or item.get("engineType") or ""
                    )
                    if engine_type not in DATABASE_ENGINES:
                        totals["raw_occurrences_skipped"] += 1
                        continue
                    totals[f"raw_{engine_type}_occurrences"] += 1
                    batch.append(
                        normalize_item(
                            item,
                            stable_item_id(item),
                            collected_at,
                            engine_type,
                        )
                    )
                    if len(batch) >= batch_size:
                        flush_batch(data_dir, batch, totals)

        flush_batch(data_dir, batch, totals)

    payload = {
        **dict(sorted(totals.items())),
        "database_paths": {
            engine_type: str(database_path(data_dir, engine_type))
            for engine_type in DATABASE_ENGINES
        },
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
