# OpenNews Analysor

Containerized collector for selected 6551 OpenNews market information streams.

The project periodically pulls 6551 OpenNews data, deduplicates items locally, stores raw API responses and normalized JSONL archives, and writes queryable engine-specific SQLite databases under `/root/trading/data/opennews`. Runtime dependencies stay inside Docker; the host only needs Docker, Docker Compose, systemd, this project directory, and the persisted data directory.

## What It Collects

The installed timer runs two paginated searches:

| Profile | 6551 engines | Schedule | Purpose |
| --- | --- | --- | --- |
| `news-score-80` | `news` | Hourly at `:00` | Market news with AI score at least 80 |
| `listing-all` | `listing` | Hourly at `:02` | All listing-engine exchange messages without a score filter |

The current deployment does **not** collect `onchain`, `meme`, `market`, or `prediction`.

Normalized rows keep a row-level `profile` derived from `engine_type`: rows are written with `profile = "news"` or `profile = "listing"`.

Long or multi-line news text is enriched separately with DeepSeek at `:05`. The enrichment service writes a concise Chinese display title to the existing SQLite `title` field, records `title_source = "deepseek"`, and writes a Chinese summary to `summary_zh`. It never changes `text`, `raw_json`, raw snapshots, or normalized JSONL archives.

## How It Works

1. Independent systemd timers start the news and listing one-shot services.
2. News runs at `:00`; listing runs at `:02`, with independent failure and retry lifecycles.
3. The container reads `OPENNEWS_TOKEN` from the project `.env`.
4. News calls `POST /open/news_search` with `engineTypes = {"news": []}` and `score = 80`.
5. Listing calls the same endpoint separately with `engineTypes = {"listing": []}` and no `score` field.
6. Item IDs are deduplicated by engine with local state in `/root/trading/data/opennews/state/seen_ids.json`.
7. Raw pages, normalized archives, engine-specific SQLite rows, and a per-run usage record are written to `/root/trading/data/opennews`.
8. The containers exit; there is no long-running collector container.
9. At `:05`, an independent one-shot service selects eligible news from each collection window whose text exceeds 160 characters or contains multiple lines, then sends them to DeepSeek in bounded batches.
10. DeepSeek failures are retried independently and never cause the news collector to rerun or mark collection as failed.

Paging follows 6551's point model, where every returned 20 records cost 1 point. News uses `limit = 20` for one point per full page. Listing uses `limit = 100` so the API's 100-page cap can still expose the full 10,000-record window. Each adaptive profile raises the next run to its configured ceiling after a saturated run and resets only after reaching a full page of IDs that existed before the run:

```text
news limit: 20
listing limit: 100
minimum pages: 1
pages after saturation: 100
maximum API page: 100
```

Duplicates first seen within the current run are counted separately and never confirm a history boundary. The default deployment stops after one fully historical page; `--stop-on-known-item` remains available for explicit manual use but is not used by the timer.

The public OpenNews search API supports one numeric minimum `score` filter per request. Separate requests are required so news can keep `score >= 80` while listing remains complete regardless of score.

## Project Layout

```text
.
  Dockerfile
  docker-compose.yml
  scripts/opennews_collector.py
  scripts/opennews_ai.py
  scripts/opennews_enricher.py
  deploy/systemd/opennews-hourly.service
  deploy/systemd/opennews-hourly.timer
  deploy/systemd/opennews-listing-hourly.service
  deploy/systemd/opennews-listing-hourly.timer
  deploy/systemd/opennews-news-enrichment-hourly.service
  deploy/systemd/opennews-news-enrichment-hourly.timer
  .env.example
  .gitignore
  .dockerignore
```

The real `.env` file is intentionally ignored by both git and Docker build context.

## Data Layout

```text
/root/trading/data/opennews/
  databases/news.sqlite3
  databases/listing.sqlite3
  normalized/YYYY/MM/DD.jsonl
  raw/YYYY/MM/DD/opennews_<profile>_YYYYMMDD_HHMMSS.json
  usage/YYYY/MM/DD.jsonl
  state/seen_ids.json
  state/last_run.json
  state/last_run_news-score-80.json
  state/adaptive_news-score-80.json
  state/last_run_listing-all.json
  state/adaptive_listing-all.json
  state/news_enrichment.json
```

`databases/news.sqlite3` and `databases/listing.sqlite3` are the current query stores. Each database has one `items` table keyed by the upstream item ID, with structured columns plus the normalized and raw JSON payloads. The two databases deliberately allow the same upstream ID to exist independently.

`raw/` stores each API page's item payload plus all non-`data` response metadata. `normalized/` remains an append-only JSONL archive. `usage/` stores one summary per successful run, including `points_used`, boundary status, page saturation, duplicates, requested engines with no returned records, database writes, and the fetched time range.

## Normalized Fields

Each normalized JSONL row contains:

| Field | Meaning |
| --- | --- |
| `schema_version` | Normalized schema version, currently `3` |
| `id` | Stable OpenNews item ID, or a generated hash when no ID is present |
| `profile` | Row-level profile derived from `engine_type`, currently `news` or `listing` |
| `collected_at` | UTC timestamp when this collector saw the item |
| `event_date` / `event_time` | UTC partition and normalized event timestamp |
| `event_time_source` / `event_time_raw` | Raw field used for event time and its original value |
| `published_at` / `created_at` | Explicit upstream fields only; `ts` is not relabeled as publication time |
| `source` | Best available display source |
| `source_raw` / `news_type` | Upstream source and message/feed type kept separately |
| `engine_type` | 6551 engine, currently `news` or `listing` |
| `title` / `text` | Display headline and immutable upstream text; long text can receive a generated Chinese title in SQLite |
| `title_source` / `description` | `title`, `text`, or `deepseek` title provenance, plus upstream description/byline |
| `link` | Source or preview URL when available |
| `assets` | Related symbols/assets parsed from the original `coins` field |
| `asset_details` | Full per-asset `symbol`, `market_type`, `score`, `grade`, and `signal` payload |
| `score`, `grade`, `signal` | 6551 AI/impact metadata when available |
| `ai_status` | Upstream AI processing status |
| `summary_zh`, `summary_en` | Summary text when available |
| `ai_rating` | Raw AI rating object when available |
| `content_fingerprint` | Normalized text fingerprint for downstream duplicate grouping |
| `raw` | Original item payload |

Items whose AI status is `pending` or `processing` are deferred rather than marked as seen. A later `done` version can therefore be normalized with its completed ratings. JSONL partition writes are atomic and idempotent by item ID, protecting retries that occur between data and state updates.

Existing JSONL rows are not rewritten. Consumers of the archive should therefore accept legacy rows without `schema_version` and earlier schema versions alongside new schema-version-3 rows. Historical news and listing rows can be normalized into the two SQLite databases without changing the source archive:

```bash
python3 scripts/migrate_normalized_to_sqlite.py \
  --data-dir /root/trading/data/opennews
```

The migration is idempotent. It imports rows whose actual `engine_type` is `news` or `listing`, then scans immutable raw snapshots so later enriched versions of the same ID can upgrade the database row. Historical `onchain`, `market`, `meme`, and `prediction` rows are skipped.

## Configuration

Create `.env` from the example:

```bash
cp .env.example .env
chmod 600 .env
```

Required values:

```bash
OPENNEWS_TOKEN=replace_with_your_6551_token
OPENNEWS_DATA_DIR=/root/trading/data/opennews
OPENNEWS_API_BASE_URL=https://ai.6551.io
DEEPSEEK_API_KEY=replace_with_your_deepseek_api_key
DEEPSEEK_API_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

The container maps the host data directory to `/data/opennews` and overrides `OPENNEWS_DATA_DIR` inside the container.

`DEEPSEEK_API_KEY` is required only by the enrichment service. The collector services continue to work when DeepSeek is unavailable or the key is absent.

The enrichment request uses `deepseek-v4-flash` with thinking enabled at `high` reasoning effort and a 32,768-token output limit. Sampling temperature is omitted because DeepSeek ignores it in thinking mode. Empty or invalid JSON responses are retried up to three times. A response ending with `finish_reason = "length"` is not written; the batch is split in half and retried, while a truncated single-item batch fails without advancing the enrichment cursor.

## Build

```bash
docker compose build opennews-news-collector
```

Rebuild the image after changing either script under `scripts/`.

## Manual Runs

Run the score-filtered news profile:

```bash
docker compose run --rm opennews-news-collector
```

The news command requests `news` with `score >= 80`:

```bash
docker compose run --rm opennews-news-collector \
  --profile news-score-80 \
  --engine-type news \
  --split-profile-by-engine \
  --min-score 80 \
  --adaptive-pages \
  --limit 20 \
  --min-pages 1 \
  --max-pages 100 \
  --no-stop-on-known-item \
  --stop-after-known-pages 1
```

Run the unfiltered listing profile:

```bash
docker compose run --rm opennews-listing-collector
```

The listing command deliberately omits `--min-score`:

```bash
docker compose run --rm opennews-listing-collector \
  --profile listing-all \
  --engine-type listing \
  --split-profile-by-engine \
  --adaptive-pages \
  --limit 100 \
  --min-pages 1 \
  --initial-pages 100 \
  --max-pages 100 \
  --no-stop-on-known-item \
  --stop-after-known-pages 1
```

Run news without writing new records:

```bash
docker compose run --rm opennews-news-collector \
  --profile news-score-80 \
  --engine-type news \
  --split-profile-by-engine \
  --min-score 80 \
  --adaptive-pages \
  --limit 20 \
  --min-pages 1 \
  --max-pages 100 \
  --no-stop-on-known-item \
  --stop-after-known-pages 1 \
  --dry-run \
  --no-raw
```

`--dry-run` still calls 6551 and may consume points; it only skips local writes.

Run a bounded 2,000-record score-filtered recovery scan without changing the default adaptive state:

```bash
docker compose run --rm opennews-news-collector \
  --profile backfill-news-score-80 \
  --engine-type news \
  --split-profile-by-engine \
  --min-score 80 \
  --limit 20 \
  --max-pages 100 \
  --no-stop-on-known-item \
  --stop-after-known-pages 0
```

This scan costs at most 100 points under the documented point model.

The API accepts at most page 100. Listing uses `limit = 100` so its initial 100-page scan covers the full 10,000-record search window.

Continue a news recovery scan with an overlapping final ten-page range:

```bash
docker compose run --rm opennews-news-collector \
  --profile backfill-news-score-80 \
  --engine-type news \
  --split-profile-by-engine \
  --min-score 80 \
  --limit 20 \
  --start-page 91 \
  --max-pages 10 \
  --no-stop-on-known-item \
  --stop-after-known-pages 0
```

This example scans pages 91-100. Requests beyond page 100 are capped locally because the API otherwise repeats page 100.

Run DeepSeek enrichment manually after setting `DEEPSEEK_API_KEY` in `.env`:

```bash
docker compose run --rm opennews-news-enricher
```

On its first run, the enricher starts with the latest completed news collection instead of backfilling the full historical database. It advances a durable collection cursor only after every eligible row in the window succeeds, so retries cannot permanently skip older rows. Short headline-like text remains unchanged with `title_source = "text"`. Existing upstream titles and Chinese summaries are preserved, and a later collector refresh cannot overwrite a generated title or summary.

## systemd Deployment

Install or update the timers:

```bash
cp deploy/systemd/opennews-hourly.service /etc/systemd/system/
cp deploy/systemd/opennews-hourly.timer /etc/systemd/system/
cp deploy/systemd/opennews-listing-hourly.service /etc/systemd/system/
cp deploy/systemd/opennews-listing-hourly.timer /etc/systemd/system/
cp deploy/systemd/opennews-news-enrichment-hourly.service /etc/systemd/system/
cp deploy/systemd/opennews-news-enrichment-hourly.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now opennews-hourly.timer
systemctl enable --now opennews-listing-hourly.timer
systemctl enable --now opennews-news-enrichment-hourly.timer
```

Check timer status:

```bash
systemctl list-timers --all --no-pager 'opennews-*.timer'
```

Start either collector manually through systemd:

```bash
systemctl start opennews-hourly.service
systemctl start opennews-listing-hourly.service
systemctl start opennews-news-enrichment-hourly.service
```

Read service logs:

```bash
journalctl -u opennews-hourly.service -n 80 --no-pager
journalctl -u opennews-listing-hourly.service -n 80 --no-pager
journalctl -u opennews-news-enrichment-hourly.service -n 80 --no-pager
```

## Useful State Files

```bash
cat /root/trading/data/opennews/state/last_run_news-score-80.json
cat /root/trading/data/opennews/state/adaptive_news-score-80.json
cat /root/trading/data/opennews/state/last_run_listing-all.json
cat /root/trading/data/opennews/state/adaptive_listing-all.json
cat /root/trading/data/opennews/state/news_enrichment.json
tail -n 1 /root/trading/data/opennews/usage/$(date -u +%Y/%m/%d).jsonl
```

`last_run_*.json` shows what the last run fetched. `adaptive_*.json` shows the current and next page limits. A run with `saturated = true` did not confirm a historical or empty-page boundary and should be monitored until a later run reports `boundary_confirmed = true`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests cover same-run duplicates, historical IDs, fully historical pages, point accounting, recovery page offsets, saturated-run recovery, score-filtered request construction, DeepSeek response validation and truncation recovery, news enrichment updates, durable cursors, and preservation of generated fields during later collector refreshes.

## Downstream Reading Tips

For U.S. stock and macro use cases, start with:

- `profile = news`
- `engine_type = news`
- `source` in higher-signal sources such as `Reuters`, `Bloomberg`, `6551Tradfi`, `6551News`, `jin10`, `CNBC`, `Financial Times`, `Fox Business`, `Nasdaq`, `GlobeNewswire`, `PRNewswire`, or `Business Wire`
- `assets` containing stock tickers or internal `XYZ-*` asset tags
- `score`, `grade`, and `signal` for priority ranking

For exchange listing and delisting use cases, start with:

- `profile = listing`
- `engine_type = listing`
- `asset_details` for asset-level score, grade, signal, and market type

## Git And Secrets

Do not commit `.env`. It is ignored by:

- `.gitignore`
- `.dockerignore`

Before pushing, verify:

```bash
git status --short
git check-ignore -v .env
```

Only `.env.example` should be committed.
