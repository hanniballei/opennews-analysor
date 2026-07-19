# OpenNews Analysor

Containerized collector for selected 6551 OpenNews market information streams.

The project periodically pulls 6551 OpenNews data, deduplicates items locally, and stores both raw API responses and normalized JSONL records under `/root/trading/data/opennews`. Runtime dependencies stay inside Docker; the host only needs Docker, Docker Compose, systemd, this project directory, and the persisted data directory.

## What It Collects

The installed timer runs one paginated news search:

| Profile | 6551 engines | Schedule | Purpose |
| --- | --- | --- | --- |
| `news-score-50` | `news` | Hourly at `:00` | Market news with AI score at least 50 |

The current deployment does **not** collect `onchain`, `meme`, `market`, `listing`, or `prediction`.

Normalized rows keep a row-level `profile` derived from `engine_type`: collected rows are written with `profile = "news"`.

## How It Works

1. A systemd timer starts one hourly one-shot service on schedule.
2. The service runs one `docker compose run --rm opennews-collector` command.
3. The container reads `OPENNEWS_TOKEN` from the project `.env`.
4. Each page calls `POST /open/news_search` with `engineTypes = {"news": []}` and `score = 50`.
5. Item IDs are deduplicated with local state in `/root/trading/data/opennews/state/seen_ids.json`.
6. Raw pages, normalized records, and a per-run usage record are written to `/root/trading/data/opennews`.
7. The container exits; there is no long-running collector container.

Paging is optimized for 6551's point model, where every returned 20 records cost 1 point. The collector uses `limit = 20` so each full page maps to one point. It starts with one page, raises the next run directly to the configured ceiling after a saturated run, and resets only after reaching a full page of IDs that existed before the run:

```text
limit: 20
min pages: 1
pages after saturation: 100
max pages: 100
```

Duplicates first seen within the current run are counted separately and never confirm a history boundary. The default deployment stops after one fully historical page; `--stop-on-known-item` remains available for explicit manual use but is not used by the timer.

The public OpenNews search API supports a numeric minimum `score` filter but does not document a single-query option for `score >= 50 OR score is missing`. The installed deployment therefore requests only news with `score >= 50`.

## Project Layout

```text
.
  Dockerfile
  docker-compose.yml
  scripts/opennews_collector.py
  deploy/systemd/opennews-hourly.service
  deploy/systemd/opennews-hourly.timer
  .env.example
  .gitignore
  .dockerignore
```

The real `.env` file is intentionally ignored by both git and Docker build context.

## Data Layout

```text
/root/trading/data/opennews/
  normalized/YYYY/MM/DD.jsonl
  raw/YYYY/MM/DD/opennews_<profile>_YYYYMMDD_HHMMSS.json
  usage/YYYY/MM/DD.jsonl
  state/seen_ids.json
  state/last_run.json
  state/last_run_news-score-50.json
  state/adaptive_news-score-50.json
```

`raw/` stores each API page's item payload plus all non-`data` response metadata. `normalized/` stores one JSON object per line for downstream processing. `usage/` stores one summary per successful run, including `points_used`, boundary status, page saturation, duplicates, requested engines with no returned records, and the fetched time range.

## Normalized Fields

Each normalized JSONL row contains:

| Field | Meaning |
| --- | --- |
| `id` | Stable OpenNews item ID, or a generated hash when no ID is present |
| `profile` | Row-level profile derived from `engine_type`, currently `news` |
| `collected_at` | UTC timestamp when this collector saw the item |
| `event_date` | Date partition derived from item event time |
| `published_at` | Source publication time when available |
| `created_at` | 6551/source creation time when available |
| `source` | Source name such as `Reuters`, `Bloomberg`, `jin10`, `binance`, etc. |
| `engine_type` | 6551 engine, currently `news` |
| `title` / `text` | Headline or text content |
| `link` | Source or preview URL when available |
| `assets` | Related symbols/assets parsed from the original `coins` field |
| `score`, `grade`, `signal` | 6551 AI/impact metadata when available |
| `summary_zh`, `summary_en` | Summary text when available |
| `ai_rating` | Raw AI rating object when available |
| `raw` | Original item payload |

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
```

The container maps the host data directory to `/data/opennews` and overrides `OPENNEWS_DATA_DIR` inside the container.

## Build

```bash
docker compose build opennews-collector
```

Rebuild the image after changing `scripts/opennews_collector.py`.

## Manual Runs

Run the default score-filtered news profile:

```bash
docker compose run --rm opennews-collector
```

The default command requests `news` with `score >= 50` in one 6551 API search:

```bash
docker compose run --rm opennews-collector \
  --profile news-score-50 \
  --engine-type news \
  --split-profile-by-engine \
  --min-score 50 \
  --adaptive-pages \
  --limit 20 \
  --min-pages 1 \
  --max-pages 100 \
  --no-stop-on-known-item \
  --stop-after-known-pages 1
```

Run without writing new records:

```bash
docker compose run --rm opennews-collector \
  --profile news-score-50 \
  --engine-type news \
  --split-profile-by-engine \
  --min-score 50 \
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
docker compose run --rm opennews-collector \
  --profile backfill-score-50 \
  --engine-type news \
  --split-profile-by-engine \
  --min-score 50 \
  --limit 20 \
  --max-pages 100 \
  --no-stop-on-known-item \
  --stop-after-known-pages 0
```

This scan costs at most 100 points under the documented point model.

Continue a recovery scan without paying for earlier pages again:

```bash
docker compose run --rm opennews-collector \
  --profile backfill-score-50 \
  --engine-type news \
  --split-profile-by-engine \
  --min-score 50 \
  --limit 20 \
  --start-page 91 \
  --max-pages 410 \
  --no-stop-on-known-item \
  --stop-after-known-pages 0
```

This example deliberately overlaps pages 91-100 and then scans through page 500. It costs at most 410 points and covers the API's current 10,000-record search window.

## systemd Deployment

Install or update the timers:

```bash
cp deploy/systemd/opennews-hourly.service /etc/systemd/system/
cp deploy/systemd/opennews-hourly.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now opennews-hourly.timer
```

Check timer status:

```bash
systemctl list-timers --all --no-pager 'opennews-*.timer'
```

Start the score-filtered collector manually through systemd:

```bash
systemctl start opennews-hourly.service
```

Read service logs:

```bash
journalctl -u opennews-hourly.service -n 80 --no-pager
```

## Useful State Files

```bash
cat /root/trading/data/opennews/state/last_run_news-score-50.json
cat /root/trading/data/opennews/state/adaptive_news-score-50.json
tail -n 1 /root/trading/data/opennews/usage/$(date -u +%Y/%m/%d).jsonl
```

`last_run_*.json` shows what the last run fetched. `adaptive_*.json` shows the current and next page limits. A run with `saturated = true` did not confirm a historical or empty-page boundary and should be monitored until a later run reports `boundary_confirmed = true`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests cover same-run duplicates, historical IDs, fully historical pages, point accounting, recovery page offsets, saturated-run recovery, and score-filtered request construction.

## Downstream Reading Tips

For U.S. stock and macro use cases, start with:

- `profile = news`
- `engine_type = news`
- `source` in higher-signal sources such as `Reuters`, `Bloomberg`, `6551Tradfi`, `6551News`, `jin10`, `CNBC`, `Financial Times`, `Fox Business`, `Nasdaq`, `GlobeNewswire`, `PRNewswire`, or `Business Wire`
- `assets` containing stock tickers or internal `XYZ-*` asset tags
- `score`, `grade`, and `signal` for priority ranking

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
