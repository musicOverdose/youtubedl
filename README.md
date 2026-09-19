# 🚀 Telegram YouTube Downloader

<div align="center">

<img src="https://img.shields.io/badge/Made_by-Farzad_(@MusicOverdose)-indigo?style=for-the-badge" alt="Made by Farzad" />
<img src="https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.13" />
<img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Ready" />
<img src="https://img.shields.io/badge/FastAPI-0.115-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
<img src="https://img.shields.io/badge/aiogram-3.x-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white" alt="aiogram 3" />
<img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="MIT License" />

<p align="center">
  <b>A production-grade, self-hosted Telegram bot and administration web suite for downloading YouTube videos, MP3 audio, and subtitles with AI-powered Persian translation.</b>
</p>

</div>

---

## 🌟 Key Features

- **Quality-First Telegram UX**:
  - Dynamically discovers actual available resolutions (`2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`) directly from YouTube source streams.
  - Sends a clean photo preview with the **Video Title only** as the caption.
  - Initial menu exposes source qualities, `🎵 MP3`, and `💬 Subtitle`.
- **Strict Exact Resolution (No Downgrading)**:
  - Enforces `height == requested_height`. If a selected resolution becomes unavailable, the request is safely rejected with guidance to select from current stream qualities. Never silently degrades resolution.
- **Two-Step Codec Selection**:
  - Resolution is selected first, then seamlessly switches to `🎬 H.264 / AAC`, `📦 H.265 / AAC`, and `⬅️ Back`.
- **Intelligent Stream Copying (CPU Optimized)**:
  - Inspects downloaded streams with `ffprobe` and prefers stream copying (`-c copy`) whenever the source already matches target codecs and MP4 container. Avoids CPU-intensive transcoding on lightweight host servers.
- **Private Telegram Channel Media Cache**:
  - Uses a designated private Telegram channel as persistent media storage.
  - Exact cache hits bypass queues, workers, yt-dlp, and FFmpeg entirely, delivering files instantly via Telegram `copyMessage`.
- **Zero Permanent Disk Waste**:
  - Files are processed in `/tmp/ytdl/<job_id>` and automatically cleaned up immediately after verified Telegram cache upload, failure, or cancellation.
- **Authoritative Must-Join Channel Guard**:
  - Verifies that users are active members of all configured required channels before allowing URL intake, callbacks, or media delivery.
  - If a user leaves a required channel at any point, access is revoked immediately.
- **Persistent Redis FIFO Queue & Race-Safe Concurrency**:
  - Queue state survives container restarts with Redis Append-Only File (`appendonly yes`).
  - Concurrency limit (`MAX_ACTIVE_JOBS`, default 1) is enforced atomically via Redis Lua scripts to eliminate race conditions.
  - Queue positions are derived dynamically based on waiting jobs ahead.
- **Duplicate Job Coalescing**:
  - Multiple users requesting the same uncached video bind to one underlying download job and share a single queue position.
- **Subtitles & OpenAI-Compatible Translation**:
  - Supports `🇬🇧 English` (auto-generated or manual) converted to clean SRT without AI.
  - Supports `🇮🇷 Persian` translated from English SRT via any OpenAI-compatible API endpoint (OpenAI, OpenRouter, Groq, Ollama, etc.) with strict timestamp and sequence preservation.
- **Optional YouTube Cookies**:
  - Upload or paste Netscape format `cookies.txt` via Web Admin with format validation, atomic file replacement, hot-reload, and zero credential leakage.
- **Full-Featured Web Administration Panel**:
  - Modern, responsive SPA dashboard on port `8085` protected by Argon2id password hashing and secure HTTP-only session cookies.
- **Portainer & Docker Compose Ready**:
  - 1-click deployment via Portainer Stack or standard Docker Compose.

---

## 🏗️ Architecture

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
    
    subgraph Processing Pipeline
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

## 💻 System Sizing & Resource Efficiency

Designed to run smoothly even on modest cloud servers:

| Resource | Recommended Baseline | Notes |
|---|---|---|
| **CPU** | 2 Cores | `MAX_ACTIVE_JOBS=1` default avoids high CPU load during FFmpeg transcoding |
| **RAM** | 2 GB – 4 GB | Memory footprint across all 5 containers is typically under 800 MB |
| **Disk** | 20 GB – 80 GB | Completed media is stored on Telegram; temp storage is bounded (`MAX_TEMP_STORAGE_GB=30`) |
| **OS** | Linux (Debian, Ubuntu, AlmaLinux, Rocky) | Docker Engine 24+ & Docker Compose v2+ |

---

## 🚀 Quick Start & Deployment

### 1. Prerequisites
1. **Telegram Bot Token**: Create a bot with [@BotFather](https://t.me/BotFather) and save the API token.
2. **Private Cache Channel**: Create a private Telegram channel, add your bot, and grant it **Administrator** privileges (post, edit, delete messages). Note the numeric channel ID (e.g. `-1001234567890`).
3. **Telegram API Credentials**: Obtain `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org).

---

### Option A: Portainer Stack Deployment (Recommended)

1. Open your Portainer Web UI.
2. Navigate to **Stacks** -> **Add stack**.
3. Choose **Repository** and provide:
   - **Repository URL**: `https://github.com/musicOverdose/youtubedl.git`
   - **Repository reference**: `refs/heads/main`
   - **Compose path**: `docker-compose.yml`
4. Under **Environment variables**, set the required values:
   ```env
   BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
   TELEGRAM_API_ID=1234567
   TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
   TELEGRAM_CACHE_CHANNEL_ID=-1001234567890
   WEB_PORT=8085
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=ChangeThisSecurePassword123!
   SECRET_KEY=generate_a_random_64_character_string_for_sessions
   MAX_ACTIVE_JOBS=1
   MAX_VIDEO_DURATION_SECONDS=7200
   ```
5. Click **Deploy the stack**.

---

### Option B: Docker Compose CLI Deployment

```bash
# 1. Clone repository
git clone https://github.com/musicOverdose/youtubedl.git /opt/youtubedl
cd /opt/youtubedl

# 2. Configure environment
cp .env.example .env
nano .env

# 3. Start services
docker compose up -d --build

# 4. Check status & logs
docker compose ps
docker compose logs -f
```

---

## 🎛️ Web Administration Panel (`:8085`)

Access the management panel at `http://<your-server-ip>:8085`.

```
┌─────────────────────────────────────────────────────────────┐
│ ⚡ YTDL Admin Panel                                         │
├───────────────┬─────────────────────────────────────────────┤
│ 📊 Dashboard  │ CPU: 12% | RAM: 1.2/4GB | Disk: 24/80GB    │
│ ⏳ Queue      │ Active: 1/1 | Waiting: 2 | Status: Normal   │
│ 📁 Jobs       │ Total: 342 | Success: 338 | Failed: 4       │
│ ⚡ Cache      │ Stored: 280 items | Hit Rate: 78.4%         │
│ 👥 Users      │ Active: 154 users | Banned: 1               │
│ 🔒 Must Join  │ Enabled: Yes | Required Channels: 2         │
│ 📺 YouTube    │ Default Max: 1080p | Playlists: Disabled    │
│ 🍪 Cookies    │ Configured: Yes | Netscape Validated        │
│ 🤖 AI Trans   │ Provider: OpenAI-compatible (gpt-4o-mini)   │
│ ⚙️ Settings   │ Max Duration: 02:00:00 | Safety Limits      │
│ 🖥️ System     │ Health: OK | PostgreSQL: OK | Redis: OK     │
│ 📝 Logs       │ Real-time structured system & worker logs   │
│ 🛡️ Audit Log  │ Security audit trail of admin actions       │
└───────────────┴─────────────────────────────────────────────┘
```

### Highlights:
- **Queue Controls**: Pause/Resume the global queue, adjust `MAX_ACTIVE_JOBS` on-the-fly without container restarts, and cancel or retry tasks.
- **Must-Join Management**: Add channels via Chat ID/Username/Invite Link; automatically verifies the bot is an Administrator before enabling.
- **Cookie Tool**: Paste or upload `cookies.txt`, test extraction against a URL, and delete without leaking secrets.
- **AI Configuration**: Test OpenAI-compatible subtitle translation and verify SRT timestamp integrity with 1-click test tool.

---

## 💾 Backup Recommendations

| Target | Backup Required? | Notes |
|---|---|---|
| **PostgreSQL** | **YES** | Contains user records, channel rules, cache keys, and audit logs |
| **Configuration** | **YES** | `.env`, cookies, and application settings |
| **Media Files** | **NO** | Completed media is permanently preserved in the private Telegram cache channel |

### Database Backup Example (Cron)
```bash
0 3 * * * docker exec ytdl_postgres pg_dump -U ytdl_user ytdl_db | gzip > /backups/ytdl_db_$(date +\%F).sql.gz
```

---

## 🧪 Automated Testing

The project includes a 27-test automated test suite:

```bash
# Run tests inside python environment:
pytest -v
```

### Coverage:
- URL extraction and canonicalization
- Dynamic quality extraction, sorting, and deduplication
- Exact quality enforcement (no fallback)
- Stream-copy vs transcode decision logic
- Subtitle extraction, SRT parsing, and validation
- Deterministic cache key generation and invalidation
- Redis FIFO queue ordering, derived position calculation, and atomic concurrency limits
- Must-Join multi-channel enforcement, membership state transitions, and rejoin delivery
- Cookie Netscape format validation and atomic file writing
- Argon2id password hashing, session tokens, and security redaction.

---

## 📄 License

This project is licensed under the MIT License.
