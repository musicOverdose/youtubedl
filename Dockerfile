# ==============================================================================
# Production Multi-Stage Dockerfile for Telegram YouTube Downloader
# Designed for Debian 13 VPS (2 CPU cores, 4 GB RAM, 80 GB HDD)
# ==============================================================================

FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TEMP_DIR=/tmp/ytdl

# Install runtime system dependencies: FFmpeg, curl, unzip, ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install Deno JS Runtime (recommended modern engine for yt-dlp challenges)
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && mv /root/.deno/bin/deno /usr/local/bin/deno \
    && rm -rf /root/.deno \
    && deno --version

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Create temporary media directory
RUN mkdir -p /tmp/ytdl /config/secrets && chmod -R 777 /tmp/ytdl

# ==============================================================================
# Target: Web Administration Panel
# ==============================================================================
FROM base AS web
EXPOSE 8085
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8085/health || exit 1
CMD ["uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8085"]

# ==============================================================================
# Target: Telegram Bot
# ==============================================================================
FROM base AS bot
CMD ["python3", "-m", "src.bot.main"]

# ==============================================================================
# Target: Background Worker (yt-dlp, FFmpeg, Deno, AI translation)
# ==============================================================================
FROM base AS worker
CMD ["python3", "-m", "src.worker.main"]
