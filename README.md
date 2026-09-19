# Telegram YouTube Downloader (Self-Hosted Production Stack)

A production-ready, private, self-hosted Telegram bot and administration panel for downloading YouTube videos, MP3 audio, and subtitles (with AI-powered Persian translation).

Designed specifically for Debian 13 VPS environments with **2 CPU cores, 4 GB RAM, and 80 GB HDD**.

---

## Key Highlights

- **Quality-First Telegram UX**: Dynamic resolution discovery (2160p, 1440p, 1080p, 720p, 480p, 360p) based on actual YouTube source streams.
- **Strict Exact Resolution**: Never silently downgrades or upgrades quality (`height == requested_height`).
- **Two-Step Codec Selection**: Video resolution selected first, followed by clean `🎬 H.264 / AAC` or `📦 H.265 / AAC` selection.
- **No Unnecessary Transcoding**: Probes source streams with `ffprobe` and prefers stream copying (`-c copy`) whenever the source already matches target codecs and MP4 container, preventing CPU spikes on 2-core VPS.
- **Private Telegram Channel Media Cache**: All finished media is uploaded to a designated private Telegram cache channel. Exact cache hits bypass queue, worker, download, and disk entirely, delivering immediately via `copyMessage`.
- **Zero Permanent Completed Media on VPS**: Files in `/tmp/ytdl/<job_id>` are automatically deleted after successful Telegram cache upload, failure, or cancellation.
- **Authoritative Must-Join Channel Guard**: Strict server-side verification ensures users remain members of all configured required channels. If a user leaves, access is revoked immediately.
- **Persistent Redis FIFO Queue**: Powered by Redis with Append-Only File (`appendonly yes`). Dynamic runtime concurrency control (`MAX_ACTIVE_JOBS`, default 1).
- **Duplicate Job Coalescing**: Multiple users requesting the same uncached video share one underlying job and one queue position.
- **Subtitles & AI Translation**: Supports English (auto or manual) and Persian (AI-translated English using any OpenAI-compatible API).
- **Optional YouTube Cookies**: Upload or paste `cookies.txt` with Netscape format validation, atomic write, hot-reload, and zero credential leakage.
- **Modern Web Administration Panel**: FastAPI + clean, responsive dashboard on port `8085` protected with Argon2id session authentication.
- **Portainer Stack Ready**: 1-click deployment via Portainer Stack or standard Docker Compose.

---

## Production Architecture

```mermaid
graph TD
    User([Telegram User]) <-->|Commands & Callbacks| Bot[Bot Service (Aiogram 3)]
    Admin([Administrator]) <-->|HTTPS/Web| Web[Web Admin (FastAPI + SPA)]
    
    subgraph Storage & Queues
        PG[(PostgreSQL 16)]
        Redis[(Redis 7 AOF Queue)]
        SecretsVol[Secrets Volume (/config/secrets)]
        TempVol[Temp Storage (/tmp/ytdl)]
    end
    
    subgraph Execution
        Worker[Worker (yt-dlp + Deno + FFmpeg)]
    end
    
    subgraph Telegram Infrastructure
        CacheChannel[Private Telegram Cache Channel]
        TelegramAPI[Telegram Bot API / Local Bot API]
    end
    
    Bot <--> Redis
    Bot <--> PG
    Bot <--> TelegramAPI
    
    Web <--> PG
    Web <--> Redis
    Web --> SecretsVol
    
    Worker <--> Redis
    Worker <--> PG
    Worker <--> TempVol
    Worker --> CacheChannel
    Worker <--> TelegramAPI
```

---

## Hardware Specifications & Resource Tuning

| Resource | Value | Notes |
|---|---|---|
| **Operating System** | Debian 13 (Trixie) / Debian 12 | Linux kernel 6.x |
| **CPU Cores** | 2 Cores | `MAX_ACTIVE_JOBS=1` default ensures smooth FFmpeg operations without host locking |
| **Memory (RAM)** | 4 GB | Low memory footprint (<800MB total across all 5 containers) |
| **Storage (HDD)** | 80 GB | Completed media stored on Telegram; `/tmp/ytdl` capped at 30 GB max |

---

## Deployment Guide (Portainer Stack)

### Step 1: Create Telegram Bot with BotFather
1. Open Telegram and search for `@BotFather`.
2. Send `/newbot` and follow the instructions to choose a name and username.
3. Save the generated **HTTP API Token** (`BOT_TOKEN`).

### Step 2: Create Private Telegram Cache Channel
1. Create a new **Private Channel** in Telegram (e.g., `My Media Cache`).
2. Add your newly created bot to the channel and promote it to **Administrator** (ensure permissions: Post Messages, Edit Messages, Delete Messages).
3. Obtain the numeric channel ID (e.g. `-1001234567890`) using `@username_to_id_bot` or by forwarding a message from the channel to `@JsonDumpBot`.

### Step 3: Obtain Telegram API ID & Hash
1. Log in to [https://my.telegram.org](https://my.telegram.org).
2. Go to **API development tools** and create a new application.
3. Note your `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

### Step 4: Deploy Stack via Portainer
1. Open your Portainer Web UI on your Debian 13 VPS.
2. Go to **Stacks** -> **Add stack**.
3. Name your stack (e.g. `ytdl`).
4. Select **Repository** (enter your GitHub repository URL) OR select **Web editor** and paste the contents of `docker-compose.yml`.
5. Under **Environment variables**, populate the required values from `.env.example`:
   ```env
   BOT_TOKEN=your_bot_token_here
   TELEGRAM_API_ID=your_api_id
   TELEGRAM_API_HASH=your_api_hash
   TELEGRAM_CACHE_CHANNEL_ID=-1001234567890
   WEB_PORT=8085
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=ChangeThisStrongPassword123!
   SECRET_KEY=generate_a_random_64_character_string_for_sessions
   MAX_ACTIVE_JOBS=1
   MAX_VIDEO_DURATION_SECONDS=7200
   ```
6. Click **Deploy the stack**.
7. Portainer will pull the base images, build the application containers, and initialize the stack.

---

## Command-Line Deployment (Docker Compose)

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/youtubedl.git /opt/youtubedl
cd /opt/youtubedl

# 2. Configure environment
cp .env.example .env
nano .env

# 3. Start the stack
docker compose up -d --build

# 4. View logs
docker compose logs -f
```

---

## Web Administration Walkthrough (`http://your-vps-ip:8085`)

Open your browser and navigate to `http://<your-vps-ip>:8085`.
Log in with your configured `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

### 1. Dashboard
Displays live CPU %, RAM usage, Disk usage, temporary storage directory size, active job counters, queue lengths, cache hit ratios, and stack component versions (`yt-dlp`, `yt-dlp-ejs`, `FFmpeg`, `Deno`, `Python`).

### 2. Queue & Concurrency
- View active processing jobs with real-time percentage, speed, and ETA.
- View waiting jobs in strict FIFO order with dynamically derived queue positions (`#1`, `#2`, etc.).
- **Pause Queue**: Pauses dispatch of new jobs while allowing in-flight jobs to finish.
- **Resume Queue**: Resumes automated processing.
- **Dynamic Concurrency**: Change `MAX_ACTIVE_JOBS` (1, 2, 3...) at runtime without container restarts.

### 3. Must-Join Channel System
- Toggle the Must-Join requirement ON or OFF.
- Add required channels by Chat ID, Title, and Username or Invite Link.
- Authoritatively tests that the bot is an Administrator in the channel before allowing it to be enabled.

### 4. YouTube Cookies
- **Upload cookies.txt** or **Paste cookies.txt** directly into the secure textarea.
- Format validator ensures standard Netscape 7-field compliance.
- Atomically replaces active cookie file with zero downtime.
- **Test Cookies**: Run safe metadata extraction against a YouTube URL without downloading media.
- Cookie content is never exposed via web APIs, logs, or Telegram.

### 5. AI Subtitle Translation
- Toggle Persian AI Translation ON or OFF.
- Configure any OpenAI-compatible API endpoint (OpenAI, OpenRouter, Groq, Ollama, etc.).
- API keys are masked in the UI.
- Test connection and SRT preservation with 1-click test button.

### 6. Settings
- Max video duration (default 7200s = 02:00:00). Videos exceeding this limit are rejected before queueing.
- Allow unknown duration toggle.
- Cache hit duration limit bypass toggle.
- Per-user concurrent and queued limits.

---

## VPS Backup Strategy

| Component | Backup Needed? | Notes |
|---|---|---|
| **PostgreSQL Database** | **YES** | Contains user history, cache keys, channel configurations, and audit logs. |
| **Settings & Secrets** | **YES** | Contains `.env`, cookies, and application configuration. |
| **Completed Media Files** | **NO** | Completed media is stored permanently inside the private Telegram cache channel. |

### Automated Database Backup Script (Cron)
```bash
# Add to crontab on your Debian VPS:
0 3 * * * docker exec ytdl_postgres pg_dump -U ytdl_user ytdl_db | gzip > /backups/ytdl_db_$(date +\%F).sql.gz
```

---

## Running Automated Tests

A comprehensive test suite of 27 unit and integration tests is included.

```bash
# Run tests inside virtualenv:
.venv/bin/pytest -v
```

Tests cover:
- URL normalization and validation
- Dynamic quality detection and descending sorting
- Strict exact-quality enforcement (no fallback)
- Stream-copy vs transcode decision logic
- Subtitle extraction and SRT parsing/validation
- Deterministic cache key generation and invalidation
- Redis FIFO queue ordering, derived position calculation, and atomic concurrency limits
- Must-Join multi-channel enforcement, membership state transitions, and rejoin delivery
- Cookie Netscape format validation and atomic file writing
- Argon2id password hashing, session tokens, and security redaction.

---

## License
MIT License. Built for private, self-hosted deployment.
