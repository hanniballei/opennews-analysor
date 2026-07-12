FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY scripts/opennews_collector.py /app/scripts/opennews_collector.py

ENTRYPOINT ["python", "/app/scripts/opennews_collector.py"]
CMD ["--data-dir", "/data/opennews", "--profile", "combined", "--engine-type", "news", "--engine-type", "onchain", "--split-profile-by-engine", "--adaptive-pages", "--limit", "20", "--min-pages", "1", "--max-pages", "100", "--no-stop-on-known-item", "--stop-after-known-pages", "1"]
