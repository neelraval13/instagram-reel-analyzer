# Pinned Python minor version. Bump deliberately; do not float.
FROM python:3.13.3-slim

# yt-dlp occasionally needs ffmpeg for muxing/remuxing.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so Docker can cache the pip install layer
# even when only app/ changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/

# Railway injects PORT env var (default 8000 as fallback).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
