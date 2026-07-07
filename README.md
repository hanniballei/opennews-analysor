# OpenNews Analysor

Containerized collector for selected 6551 OpenNews market information streams.

The project periodically pulls 6551 OpenNews data, deduplicates items locally, and stores both raw API responses and normalized JSONL records under `/root/trading/data/opennews`. Runtime dependencies stay inside Docker; the host only needs Docker, Docker Compose, systemd, this project directory, and the persisted data directory.

## What It Collects

The installed timers collect two profiles:

| Profile | 6551 engines | Schedule | Purpose |
| --- | --- | --- | --- |
| `news` | `news` | Hourly at `:00` | News, wire headlines, official channels, social/news signals |
| `onchain` | `onchain` | Hourly at `:00`, after `news` finishes | On-chain whale trade and large position events |

The current deployment does **not** collect `meme`, `market`, `listing`, or `prediction`.

## How It Works

1. A systemd timer starts one hourly one-shot service on schedule.
2. The service runs two `docker compose run --rm opennews-collector` commands sequentially: first `news`, then `onchain`.
3. The container reads `OPENNEWS_TOKEN` from the project `.env`.
4. The collector calls `POST /open/news_search` with an `engineTypes` filter.
5. News IDs are deduplicated with local state in `/root/trading/data/opennews/state/seen_ids.json`.
6. Raw pages and normalized records are written to `/root/trading/data/opennews`.
7. The container exits; there is no long-running collector container.

Paging is adaptive. Each profile starts with a small page limit and increases it only when the current run reaches the limit before seeing old items:

```text
min pages: 3
page step: 2
max pages: 20
```

This keeps normal runs shallow while still expanding coverage during busy periods.

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
  state/seen_ids.json
  state/last_run.json
  state/last_run_news.json
  state/last_run_onchain.json
  state/adaptive_news.json
  state/adaptive_onchain.json
```

`raw/` stores API pages exactly as returned by 6551. `normalized/` stores one JSON object per line for downstream processing.

## Normalized Fields

Each normalized JSONL row contains:

| Field | Meaning |
| --- | --- |
| `id` | Stable OpenNews item ID, or a generated hash when no ID is present |
| `profile` | Collector profile, currently `news` or `onchain` |
| `collected_at` | UTC timestamp when this collector saw the item |
| `event_date` | Date partition derived from item event time |
| `published_at` | Source publication time when available |
| `created_at` | 6551/source creation time when available |
| `source` | Source name such as `Reuters`, `Bloomberg`, `jin10`, `binance`, etc. |
| `engine_type` | 6551 engine, for example `news`, `onchain`, or `market` |
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

Run the default `news` profile:

```bash
docker compose run --rm opennews-collector
```

Run the `onchain` profile:

```bash
docker compose run --rm opennews-collector \
  --profile onchain \
  --engine-type onchain \
  --adaptive-pages \
  --min-pages 3 \
  --max-pages 20 \
  --page-step 2
```

Run without writing new records:

```bash
docker compose run --rm opennews-collector --dry-run --no-raw
```

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

Start both profiles manually through systemd:

```bash
systemctl start opennews-hourly.service
```

Read service logs:

```bash
journalctl -u opennews-hourly.service -n 80 --no-pager
```

## Useful State Files

```bash
cat /root/trading/data/opennews/state/last_run_news.json
cat /root/trading/data/opennews/state/last_run_onchain.json
cat /root/trading/data/opennews/state/adaptive_news.json
cat /root/trading/data/opennews/state/adaptive_onchain.json
```

`last_run_*.json` shows what the last run fetched. `adaptive_*.json` shows the current page limit and the next run's page limit.

## Downstream Reading Tips

For U.S. stock and macro use cases, start with:

- `profile = news`
- `engine_type = news`
- `source` in higher-signal sources such as `Reuters`, `Bloomberg`, `6551Tradfi`, `6551News`, `jin10`, `CNBC`, `Financial Times`, `Fox Business`, `Nasdaq`, `GlobeNewswire`, `PRNewswire`, or `Business Wire`
- `assets` containing stock tickers or internal `XYZ-*` asset tags
- `score`, `grade`, and `signal` for priority ranking

For on-chain signals, use:

- `profile = onchain`
- `engine_type = onchain`

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
